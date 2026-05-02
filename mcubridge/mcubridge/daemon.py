#!/usr/bin/env python3
"""Async orchestrator for the Arduino MCU Bridge v2 daemon.

This module contains the main entry point and orchestration logic for the
MCU Bridge daemon, which manages communication between OpenWrt Linux and
the Arduino MCU over serial and MQTT.

[SIL-2 COMPLIANCE]
The daemon implements robust error handling:
- Deterministic startup (Fail-Fast on missing deps)
- Task supervision with automatic restart and backoff
- Fatal exception handling for unrecoverable serial errors
- Graceful shutdown on SIGTERM/SIGINT
- Status file cleanup on exit
"""

from __future__ import annotations

import asyncio
import os
import sys
from collections.abc import Awaitable, Callable
from typing import Any, cast

import tenacity

# [SIL-2] Deterministic Import: uvloop is MANDATORY for performance on OpenWrt.
import structlog
import uvloop
import aiomqtt
from functools import partial
import logging

from mcubridge.config.const import (
    SUPERVISOR_DEFAULT_MAX_BACKOFF,
    SUPERVISOR_DEFAULT_MIN_BACKOFF,
)
from mcubridge.config.logging import configure_logging
from mcubridge.config.settings import (
    RuntimeConfig,
    get_config_source,
    load_runtime_config,
)
from mcubridge.metrics import (
    PrometheusExporter,
    publish_metrics,
)
from mcubridge.security.security import verify_crypto_integrity
from mcubridge.services.handshake import SerialHandshakeFatal
from mcubridge.services.runtime import BridgeService
from mcubridge.state.context import create_runtime_state
from mcubridge.state.status import STATUS_FILE, status_writer
from mcubridge.transport import serial_link
from mcubridge.watchdog import WatchdogKeepalive
from mcubridge.mqtt.queue import (
    configure_spool,
    initialize_spool,
    enqueue_mqtt,
    stash_mqtt_message,
    flush_mqtt_spool,
)

logger = structlog.get_logger("mcubridge")


def _cleanup_child_processes() -> None:
    """Terminates all child processes spawned by this daemon using direct psutil delegation."""
    import psutil
    import contextlib

    try:
        parent = psutil.Process(os.getpid())
        children = parent.children(recursive=True)

        # 1. Terminate all
        for p in children:
            with contextlib.suppress(psutil.NoSuchProcess, ProcessLookupError):
                p.terminate()

        # 2. Wait for termination
        _, alive = psutil.wait_procs(children, timeout=3.0)

        # 3. Force kill survivors
        for p in alive:
            with contextlib.suppress(psutil.NoSuchProcess, ProcessLookupError):
                logger.warning("Force killing zombie process %d", p.pid)
                p.kill()

    except (psutil.NoSuchProcess, ProcessLookupError):
        pass
    except psutil.Error as e:
        logger.error("Error during process tree cleanup: %s", e)


class BridgeDaemon:
    """Main orchestrator for the MCU Bridge daemon services."""

    def __init__(self, config: RuntimeConfig):
        self.config = config
        self.state = create_runtime_state(config)
        self.state.config_source = get_config_source()

        configure_spool(
            self.state, self.config.mqtt_spool_dir, self.config.mqtt_queue_limit * 4
        )
        initialize_spool(self.state)

        # [SIL-2] Direct partial binding of state to the queue function
        enqueue_func = partial(enqueue_mqtt, self.state)

        self.service = BridgeService(config, self.state, enqueue_func)

        self.watchdog: WatchdogKeepalive | None = None
        self.exporter: PrometheusExporter | None = None

        if self.config.serial_shared_secret:
            logger.info("Security check passed: Shared secret is configured.")

    async def run(self) -> None:
        """Main entry point for daemon execution using native TaskGroup orchestration."""
        log = structlog.get_logger("mcubridge.daemon")

        try:
            async with self.service:
                async with asyncio.TaskGroup() as tg:
                    # 1. Serial Link (Zero-Wrapper)
                    tg.create_task(
                        self._supervise(
                            "serial-link",
                            self._run_serial_link,
                            (SerialHandshakeFatal,),
                        )
                    )

                    # 2. MQTT Link (Zero-Wrapper)
                    tg.create_task(
                        self._supervise(
                            "mqtt-link",
                            self._run_mqtt_link,
                        )
                    )

                    # 3. Status & Metrics (Periodic)
                    tg.create_task(
                        self._supervise(
                            "status-writer",
                            lambda: status_writer(
                                self.state, self.config.status_interval
                            ),
                        )
                    )
                    tg.create_task(
                        self._supervise(
                            "metrics-publisher",
                            lambda: publish_metrics(
                                self.state,
                                partial(enqueue_mqtt, self.state),
                                float(self.config.status_interval),
                            ),
                        )
                    )

                    if self.config.watchdog_enabled:
                        self.watchdog = WatchdogKeepalive(
                            interval=self.config.watchdog_interval, state=self.state
                        )
                        tg.create_task(self._supervise("watchdog", self.watchdog.run))

                    if self.config.metrics_enabled:
                        self.exporter = PrometheusExporter(
                            self.state,
                            self.config.metrics_host,
                            self.config.metrics_port,
                        )
                        tg.create_task(
                            self._supervise("prometheus-exporter", self.exporter.run)
                        )

        except* asyncio.CancelledError:
            log.info("Daemon shutdown initiated (Cancelled).")
        except* Exception as exc_group:
            for exc in exc_group.exceptions:
                log.critical("Fatal task error: %s", exc, exc_info=exc)
            raise
        finally:
            self.state.cleanup()
            _cleanup_child_processes()
            STATUS_FILE.unlink(missing_ok=True)
            log.info("MCU Bridge daemon stopped.")

    async def _run_serial_link(self) -> None:
        """Main serial run loop with reconnection and handshake logic."""
        from mcubridge.protocol.frame import Frame
        from cobs import cobs
        import contextlib

        log = structlog.get_logger("mcubridge.serial")
        log.info("Connecting to MCU on %s...", self.config.serial_port)
        await serial_link.toggle_dtr(self.config.serial_port)

        connect_baud = self.config.serial_safe_baud or 115200

        reader, writer = await serial_link.open_serial_link(
            self.config.serial_port, connect_baud
        )
        self.state.serial_writer = cast(asyncio.BaseTransport, writer)

        # [SIL-2] Bind sender directly to service's flow controller
        self.service.serial_flow.set_sender(
            partial(serial_link.write_frame, writer, self.state)
        )

        stop_event = asyncio.Event()
        negotiation_future: asyncio.Future[bool] = (
            asyncio.get_running_loop().create_future()
        )
        is_negotiating = False

        def on_packet(encoded_packet: bytes | memoryview) -> None:
            nonlocal is_negotiating
            if is_negotiating and not negotiation_future.done():
                try:
                    frame = Frame.parse(cobs.decode(bytes(encoded_packet)))
                    if (
                        frame.command_id == 0x4B
                    ):  # CMD_SET_BAUDRATE_RESP (Manually verified)
                        serial_link.switch_local_baudrate(
                            writer, self.config.serial_baud
                        )
                        negotiation_future.set_result(True)
                        return
                except (ValueError, Exception):
                    pass

            try:
                frame = Frame.parse(cobs.decode(bytes(encoded_packet)))
                asyncio.get_running_loop().create_task(
                    self.service.handle_mcu_frame(
                        frame.command_id, frame.sequence_id, frame.payload
                    )
                )
            except (ValueError, Exception) as e:
                log.warning("Discarding malformed serial packet: %s", e)
                return

            # [SIL-2] Direct metrics for RX
            nbytes = len(encoded_packet)
            self.state.serial_bytes_received += nbytes
            self.state.serial_frames_received += 1
            self.state.metrics.serial_bytes_received.inc(nbytes)
            self.state.metrics.serial_frames_received.inc()
            self.state.serial_throughput_stats.record_rx(nbytes)

        # Start reader loop
        read_task = asyncio.get_running_loop().create_task(
            serial_link.read_loop(
                reader, self.state, self.service, stop_event, cast(Any, on_packet)
            )
        )

        try:
            # 1. Negotiate baudrate if needed
            if self.config.serial_baud != connect_baud:
                is_negotiating = True
                if not await serial_link.negotiate_baudrate(
                    writer, self.state, self.config.serial_baud, negotiation_future
                ):
                    raise ConnectionError("Baudrate negotiation failed")
                is_negotiating = False

            # 2. Complete handshake via service
            await self.service.on_serial_connected()

            # 3. Keep running until read task fails or we stop
            await read_task

        finally:
            stop_event.set()
            read_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await read_task
            await self.service.on_serial_disconnected()
            writer.close()
            with contextlib.suppress(Exception):
                await writer.wait_closed()

    async def _supervise(
        self,
        name: str,
        factory: Callable[[], Awaitable[None]],
        fatal_exceptions: tuple[type[BaseException], ...] = (),
        max_restarts: int | None = None,
        min_backoff: float = SUPERVISOR_DEFAULT_MIN_BACKOFF,
        max_backoff: float = SUPERVISOR_DEFAULT_MAX_BACKOFF,
    ) -> None:
        """Lightweight supervisor with Circuit Breaker logic (SIL 2)."""
        max_consecutive_max_backoff = 10

        def _circuit_breaker(rs: tenacity.RetryCallState) -> bool:
            if not rs.outcome or not rs.outcome.failed:
                return False
            exc = rs.outcome.exception()
            if isinstance(exc, (*fatal_exceptions, asyncio.CancelledError)):
                return False
            if (
                rs.idle_for >= max_backoff
                and rs.attempt_number >= max_consecutive_max_backoff
            ):
                logger.critical(
                    "CIRCUIT BREAKER: Task '%s' tripped after repeated failures at max backoff.",
                    name,
                )
                return False
            return True

        retryer = tenacity.AsyncRetrying(
            stop=(
                tenacity.stop_after_attempt(max_restarts + 1)
                if max_restarts is not None
                else tenacity.stop_never
            ),
            wait=tenacity.wait_exponential(multiplier=min_backoff, max=max_backoff),
            retry=_circuit_breaker,
            before_sleep=lambda rs: self.state.record_supervisor_failure(
                name,
                backoff=float(rs.next_action.sleep if rs.next_action else 0.0),
                exc=rs.outcome.exception() if rs.outcome else None,
            ),
            reraise=True,
        )

        async for attempt in retryer:
            with attempt:
                await factory()

    async def _run_mqtt_link(self) -> None:
        """Main MQTT run loop with reconnection logic."""
        if not self.config.mqtt_enabled:
            logger.info("MQTT transport is DISABLED in configuration.")
            return

        tls_context = self.config.get_ssl_context()
        reconnect_delay = max(1, self.config.reconnect_delay)

        _retryable_excs = (aiomqtt.MqttError, OSError, asyncio.TimeoutError)

        def _is_retryable(e: BaseException) -> bool:
            if isinstance(e, _retryable_excs):
                return True
            if isinstance(e, BaseExceptionGroup):
                return any(
                    _is_retryable(cast(BaseException, sub))
                    for sub in cast(Any, e).exceptions
                )
            return False

        def _retry_predicate(retry_state: tenacity.RetryCallState) -> bool:
            if not retry_state.outcome or not retry_state.outcome.failed:
                return False
            exc = retry_state.outcome.exception()
            return _is_retryable(exc) if exc else False

        retryer = tenacity.AsyncRetrying(
            wait=tenacity.wait_exponential(multiplier=reconnect_delay, max=60)
            + tenacity.wait_random(0, 2),
            retry=_retry_predicate,
            before_sleep=tenacity.before_sleep_log(logger, logging.WARNING),
            after=lambda rs: self.state.metrics.retries.labels(
                component="mqtt_connect"
            ).inc(),
            reraise=True,
        )

        try:
            async for attempt in retryer:
                with attempt:
                    await self._connect_mqtt_session(tls_context)
        except asyncio.CancelledError:
            logger.info("MQTT transport stopping.")
            raise
        except (ConnectionError, OSError, asyncio.TimeoutError) as exc:
            logger.critical("MQTT transport fatal error: %s", exc)
            raise
        except BaseExceptionGroup as eg:
            for exc in eg.exceptions:
                logger.critical("MQTT transport fatal error: %s", exc)
            raise

    async def _connect_mqtt_session(self, tls_context: Any) -> None:
        from mcubridge.mqtt import build_mqtt_connect_properties
        from mcubridge.protocol.protocol import MQTT_COMMAND_SUBSCRIPTIONS, Topic
        from mcubridge.protocol.topics import topic_path

        connect_props = build_mqtt_connect_properties()

        will_topic = topic_path(self.state.mqtt_topic_prefix, Topic.SYSTEM, "status")
        will_payload = b'{"status": "offline", "reason": "unexpected_disconnect"}'
        will = aiomqtt.Will(topic=will_topic, payload=will_payload, qos=1, retain=True)

        async with aiomqtt.Client(
            hostname=self.config.mqtt_host,
            port=self.config.mqtt_port,
            username=self.config.mqtt_user or None,
            password=self.config.mqtt_pass or None,
            tls_context=tls_context,
            logger=logging.getLogger("mcubridge.mqtt.client"),
            protocol=aiomqtt.ProtocolVersion.V5,
            clean_session=None,
            will=will,
            properties=connect_props,
        ) as client:
            logger.info("Connected to MQTT broker (Paho v2/MQTTv5).")

            topics = [
                (topic_path(self.state.mqtt_topic_prefix, t, *s), int(q))
                for t, s, q in MQTT_COMMAND_SUBSCRIPTIONS
            ]
            await client.subscribe(topics)
            logger.info("Subscribed to %d command topics.", len(topics))

            await client.publish(
                will_topic, b'{"status": "online"}', qos=1, retain=True
            )

            async with asyncio.TaskGroup() as task_group:
                task_group.create_task(self._mqtt_publisher_loop(client))
                task_group.create_task(self._mqtt_subscriber_loop(client))

    async def _mqtt_publisher_loop(self, client: aiomqtt.Client) -> None:
        try:
            while True:
                await flush_mqtt_spool(self.state)
                message = await self.state.mqtt_publish_queue.get()

                topic_name = message.topic_name
                props = message.to_paho_properties()
                payload = message.payload
                qos = int(message.qos)
                retain = message.retain

                @tenacity.retry(
                    wait=tenacity.wait_exponential(multiplier=0.1, max=10),
                    retry=tenacity.retry_if_exception_type(aiomqtt.MqttError),
                    before_sleep=tenacity.before_sleep_log(logger, logging.DEBUG),
                )
                async def _reliable_publish() -> None:
                    await client.publish(
                        topic_name,
                        payload,
                        qos=qos,
                        retain=retain,
                        properties=props,
                    )

                published = False
                should_requeue = False
                try:
                    await _reliable_publish()
                    self.state.mqtt_messages_published += 1
                    self.state.metrics.mqtt_messages_published.inc()
                    published = True
                except aiomqtt.MqttError as exc:
                    logger.warning("MQTT persistent publish failure: %s", exc)
                    should_requeue = not await stash_mqtt_message(self.state, message)
                except asyncio.CancelledError:
                    should_requeue = True
                    raise
                except (OSError, RuntimeError, ValueError, TypeError) as exc:
                    logger.error("Unexpected error in MQTT publisher: %s", exc)
                    should_requeue = not await stash_mqtt_message(self.state, message)
                finally:
                    if not published and should_requeue:
                        try:
                            self.state.mqtt_publish_queue.put_nowait(message)
                        except asyncio.QueueFull:
                            await stash_mqtt_message(self.state, message)
                    self.state.mqtt_publish_queue.task_done()

        except asyncio.CancelledError:
            logger.debug("MQTT publisher loop cancelled.")
            raise

    async def _mqtt_subscriber_loop(self, client: aiomqtt.Client) -> None:
        import contextlib

        try:
            async for message in client.messages:
                try:
                    topic_str = str(message.topic)
                except (TypeError, ValueError):
                    continue

                if not topic_str:
                    continue

                try:
                    await self.service.handle_mqtt_message(message)
                except (
                    AttributeError,
                    IndexError,
                    KeyError,
                    OSError,
                    RuntimeError,
                    TypeError,
                    ValueError,
                ) as e:
                    logger.error(
                        "Error processing MQTT message on topic %s: %s", topic_str, e
                    )
        except asyncio.CancelledError:
            with contextlib.suppress(asyncio.CancelledError):
                raise
        except aiomqtt.MqttError as exc:
            logger.warning("MQTT subscriber loop interrupted: %s", exc)
            raise


def main() -> None:
    """Main entry point for the MCU Bridge daemon."""
    import argparse

    parser = argparse.ArgumentParser(description="Arduino MCU Bridge Daemon v2")
    parser.add_argument("--serial-port", help="Serial port to use")
    parser.add_argument("--serial-baud", type=int, help="Serial baud rate")
    parser.add_argument("--mqtt-host", help="MQTT host")
    parser.add_argument("--mqtt-port", type=int, help="MQTT port")
    parser.add_argument("--mqtt-tls", type=int, help="Use TLS for MQTT (0 or 1)")
    parser.add_argument("--serial-shared-secret", help="Shared secret for serial link")
    parser.add_argument(
        "--allowed-commands", help="Comma-separated list of allowed shell commands"
    )
    parser.add_argument(
        "--non-interactive", action="store_true", help="Enable non-interactive mode"
    )
    parser.add_argument("--debug", action="store_true", help="Enable debug logging")

    args = parser.parse_args()

    overrides: dict[str, Any] = {}
    if args.serial_port:
        overrides["serial_port"] = args.serial_port
    if args.serial_baud:
        overrides["serial_baud"] = args.serial_baud
    if args.mqtt_host:
        overrides["mqtt_host"] = args.mqtt_host
    if args.mqtt_port:
        overrides["mqtt_port"] = args.mqtt_port
    if args.mqtt_tls is not None:
        overrides["mqtt_tls"] = bool(args.mqtt_tls)
    if args.serial_shared_secret:
        overrides["serial_shared_secret"] = args.serial_shared_secret
    if args.non_interactive:
        overrides["non_interactive"] = True
    if args.debug:
        overrides["debug"] = True
    if args.allowed_commands:
        overrides["allowed_commands"] = (
            args.allowed_commands.split(",") if args.allowed_commands != "*" else "*"
        )

    config = load_runtime_config(overrides)
    configure_logging(config)

    if not verify_crypto_integrity():
        logger.critical("CRYPTOGRAPHIC INTEGRITY CHECK FAILED! Aborting for security.")
        sys.exit(1)

    daemon = None
    try:
        daemon = BridgeDaemon(config)
        with asyncio.Runner(loop_factory=uvloop.new_event_loop) as runner:
            runner.run(daemon.run())
    except KeyboardInterrupt:
        logger.info("Daemon interrupted by user.")
    except Exception as exc:
        logger.critical("Unhandled system error: %s", exc, exc_info=True)
        sys.exit(1)
    finally:
        if daemon is not None:
            daemon.state.cleanup()


if __name__ == "__main__":
    main()
