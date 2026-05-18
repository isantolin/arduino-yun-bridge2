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

Architecture:
    main() -> BridgeDaemon -> TaskGroup
        ├── serial-link (SerialTransport)
        ├── mqtt-link (mqtt_task)
        ├── status-writer (status_writer)
        ├── metrics-publisher (publish_metrics)
        ├── bridge-snapshots (optional)
        ├── watchdog (optional)
        ├── prometheus-exporter (optional)
"""

from __future__ import annotations

import argparse
import asyncio
import sys
from collections.abc import Awaitable, Callable
from typing import Any

import msgspec
import tenacity

# [SIL-2] Deterministic Import: uvloop is MANDATORY for performance on OpenWrt.
import structlog
import uvloop

import logging
import aiomqtt
from paho.mqtt.packettypes import PacketTypes
from paho.mqtt.properties import Properties
from mcubridge.protocol.topics import topic_path
from mcubridge.protocol.protocol import Topic, MQTT_COMMAND_SUBSCRIPTIONS


from mcubridge.config.const import (
    DEFAULT_SERIAL_SHARED_SECRET,
    SUPERVISOR_DEFAULT_MAX_BACKOFF,
    SUPERVISOR_DEFAULT_MIN_BACKOFF,
    SUPERVISOR_MAX_CONSECUTIVE_MAX_BACKOFF,
)
from mcubridge.config.logging import configure_logging
from mcubridge.config.settings import (
    RuntimeConfig,
    get_config_source,
    load_runtime_config,
)
from mcubridge.metrics import (
    PrometheusExporter,
    publish_bridge_snapshots,
    publish_metrics,
)
from mcubridge.security.security import verify_crypto_integrity
from mcubridge.services.handshake import SerialHandshakeFatal
from mcubridge.services.runtime import BridgeService
from mcubridge.state.context import create_runtime_state
from mcubridge.state.status import STATUS_FILE, status_writer
from mcubridge.transport.serial import SerialTransport
from mcubridge.watchdog import WatchdogKeepalive

logger = structlog.get_logger("mcubridge")


class BridgeDaemon:
    """Main orchestrator for the MCU Bridge daemon services.

    This class manages the lifecycle of all daemon components including
    serial communication, MQTT publishing, metrics, and optional features
    like watchdog and Prometheus exporter.

    Attributes:
        config: Runtime configuration loaded from UCI.
        state: Shared runtime state for all components.
        service: BridgeService instance handling RPC dispatch.
        watchdog: Optional watchdog keepalive task.
        exporter: Optional Prometheus metrics exporter.
    """

    def __init__(self, config: RuntimeConfig):
        """Initialize the daemon with configuration."""
        self.config = config
        self.state = create_runtime_state(config)
        self.state.config_source = get_config_source()

        # 1. Create Transports
        self.serial_transport = SerialTransport(self.config, self.state, None)

        # 2. Create Service with both transports
        self.service = BridgeService(config, self.state, self.serial_transport)

        # 3. Explicitly link transports to service
        self.serial_transport.service = self.service
        self.service.register_serial_sender(self.serial_transport.send)

        self.watchdog: WatchdogKeepalive | None = None
        self.exporter: PrometheusExporter | None = None

        if self.config.serial_shared_secret:
            logger.info("Security check passed: Shared secret is configured.")

    async def _mqtt_run(self) -> None:
        if not self.config.mqtt_enabled:
            logger.info("MQTT transport is DISABLED in configuration.")
            return

        tls_context = self.config.get_ssl_context()
        reconnect_delay = max(1, self.config.reconnect_delay)

        def _is_retryable(e: Any) -> bool:
            if isinstance(e, (aiomqtt.MqttError, OSError, asyncio.TimeoutError)):
                return True
            if isinstance(e, BaseExceptionGroup):
                return any(_is_retryable(sub) for sub in e.exceptions)  # type: ignore
            return False

        def _retry_predicate(retry_state: tenacity.RetryCallState) -> bool:
            if not retry_state.outcome or not retry_state.outcome.failed:
                return False
            exc = retry_state.outcome.exception()
            return _is_retryable(exc) if exc else False

        retryer = tenacity.AsyncRetrying(
            wait=tenacity.wait_exponential(multiplier=reconnect_delay, max=60) + tenacity.wait_random(0, 2),
            retry=_retry_predicate,
            before_sleep=tenacity.before_sleep_log(logger, logging.WARNING),
            after=lambda rs: self.state.metrics.retries.labels(component="mqtt_connect").inc(),
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
        connect_props = Properties(PacketTypes.CONNECT)
        connect_props.SessionExpiryInterval = 0
        connect_props.RequestResponseInformation = 1
        connect_props.RequestProblemInformation = 1

        if not self.config.mqtt_user:
            logger.warning("MQTT connecting without authentication (anonymous)")

        will_topic = topic_path(self.state.mqtt_topic_prefix, Topic.SYSTEM, "status")
        will = aiomqtt.Will(
            topic=will_topic,
            payload=b'{"status": "offline", "reason": "unexpected_disconnect"}',
            qos=1,
            retain=True,
        )

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
            self.service.set_mqtt_client(client)
            try:
                topics = [
                    (topic_path(self.state.mqtt_topic_prefix, t, *s), int(q)) for t, s, q in MQTT_COMMAND_SUBSCRIPTIONS
                ]
                await client.subscribe(topics)
                await client.publish(will_topic, b'{"status": "online"}', qos=1, retain=True)

                async for message in client.messages:
                    if message.topic:
                        try:
                            await self.service.handle_mqtt_message(message)
                        except (ValueError, RuntimeError, asyncio.QueueFull) as e:
                            logger.error(
                                "Error processing MQTT message",
                                topic=str(message.topic),
                                error=str(e),
                                payload_hex=(message.payload.hex() if message.payload else None),
                            )
            finally:
                self.service.set_mqtt_client(None)

    async def run(self) -> None:
        """Main entry point for daemon execution using native TaskGroup orchestration."""
        log = structlog.get_logger("mcubridge.daemon")

        try:
            async with self.service:
                async with asyncio.TaskGroup() as tg:
                    # 1. Serial Link (Critical)
                    tg.create_task(
                        self._supervise(
                            "serial-link",
                            self.serial_transport.run,
                            (SerialHandshakeFatal,),
                        )
                    )

                    # 2. MQTT Link
                    tg.create_task(
                        self._supervise(
                            "mqtt-link",
                            self._mqtt_run,
                        )
                    )

                    # 3. Status & Metrics (Periodic)
                    tg.create_task(
                        self._supervise(
                            "status-writer",
                            lambda: status_writer(self.state, self.config.status_interval),
                        )
                    )
                    tg.create_task(
                        self._supervise(
                            "metrics-publisher",
                            lambda: publish_metrics(
                                self.state,
                                self.service.enqueue_mqtt,
                                float(self.config.status_interval),
                            ),
                        )
                    )

                    # 4. Optional Features
                    if self.config.bridge_summary_interval > 0.0 or self.config.bridge_handshake_interval > 0.0:
                        tg.create_task(
                            self._supervise(
                                "bridge-snapshots",
                                lambda: publish_bridge_snapshots(
                                    self.state,
                                    self.service.enqueue_mqtt,
                                    summary_interval=float(self.config.bridge_summary_interval),
                                    handshake_interval=float(self.config.bridge_handshake_interval),
                                ),
                            )
                        )

                    if self.config.watchdog_enabled:
                        self.watchdog = WatchdogKeepalive(interval=self.config.watchdog_interval, state=self.state)
                        tg.create_task(self._supervise("watchdog", self.watchdog.run))

                    if self.config.metrics_enabled:
                        self.exporter = PrometheusExporter(
                            self.state,
                            self.config.metrics_host,
                            self.config.metrics_port,
                        )
                        tg.create_task(self._supervise("prometheus-exporter", self.exporter.run))

        except* asyncio.CancelledError:
            log.info("Daemon shutdown initiated (Cancelled).")
        except* Exception as exc_group:
            # [SIL-2] Iterative reduction for exception logging
            list(
                map(
                    lambda e: log.critical("Fatal task error: %s", e, exc_info=e),
                    exc_group.exceptions,
                )
            )
            raise
        finally:
            self.state.cleanup()
            STATUS_FILE.unlink(missing_ok=True)
            log.info("MCU Bridge daemon stopped.")

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
        # [SIL-2] Circuit Breaker: Stop after consecutive failures at max backoff
        # to prevent infinite CPU thrashing on persistent hardware failure.

        def _circuit_breaker(rs: tenacity.RetryCallState) -> bool:
            if not rs.outcome or not rs.outcome.failed:
                return False
            exc = rs.outcome.exception()
            if isinstance(exc, (*fatal_exceptions, asyncio.CancelledError)):
                return False
            # Check for consecutive max backoff hits
            if rs.idle_for >= max_backoff and rs.attempt_number >= SUPERVISOR_MAX_CONSECUTIVE_MAX_BACKOFF:
                logger.critical(
                    "CIRCUIT BREAKER: Task '%s' tripped after repeated failures at max backoff.",
                    name,
                )
                return False
            return True

        retryer = tenacity.AsyncRetrying(
            stop=(tenacity.stop_after_attempt(max_restarts + 1) if max_restarts is not None else tenacity.stop_never),
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


def _build_arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="mcubridge",
        description="Main entry point for the MCU Bridge daemon. Arduino MCU Bridge Daemon v2",
        add_help=True,
    )
    parser.add_argument("--serial-port", default=None, help="Serial port to use")
    parser.add_argument("--serial-baud", type=int, default=None, help="Serial baud rate")
    parser.add_argument("--mqtt-host", default=None, help="MQTT host")
    parser.add_argument("--mqtt-port", type=int, default=None, help="MQTT port")
    parser.add_argument("--mqtt-tls", type=int, default=None, help="Use TLS for MQTT (0 or 1)")
    parser.add_argument(
        "--serial-shared-secret",
        "--serial-shared_secret",
        dest="serial_shared_secret",
        default=None,
        help="Shared secret for serial link",
    )
    parser.add_argument(
        "--allowed-commands",
        default=None,
        help="Comma-separated list of allowed shell commands",
    )
    parser.add_argument(
        "--non-interactive",
        action="store_true",
        default=False,
        help="Enable non-interactive mode",
    )
    parser.add_argument("--debug", action="store_true", default=False, help="Enable debug logging")
    return parser


def app(argv: list[str] | None = None) -> None:
    """CLI entry point for the MCU Bridge daemon."""
    args = _build_arg_parser().parse_args(argv)

    _ac = args.allowed_commands
    overrides: dict[str, Any] = {
        k: v
        for k, v in {
            "serial_port": args.serial_port,
            "serial_baud": args.serial_baud,
            "mqtt_host": args.mqtt_host,
            "mqtt_port": args.mqtt_port,
            "mqtt_tls": bool(args.mqtt_tls) if args.mqtt_tls is not None else None,
            "serial_shared_secret": args.serial_shared_secret,
            "non_interactive": args.non_interactive or None,
            "debug": args.debug or None,
            "allowed_commands": ((_ac.split(",") if _ac != "*" else "*") if _ac else None),
        }.items()
        if v is not None
    }

    main(overrides)


def main(overrides: dict[str, Any]) -> None:
    """Run the MCU Bridge daemon with the given configuration overrides."""
    config = load_runtime_config(overrides)
    configure_logging(config)

    # [MIL-SPEC] FIPS 140-3 Power-On Self-Tests (POST)
    if not verify_crypto_integrity():
        logger.critical("CRYPTOGRAPHIC INTEGRITY CHECK FAILED! Aborting for security.")
        sys.exit(1)

    logger.info(
        "Starting MCU Bridge daemon. Serial: %s@%d MQTT: %s:%d",
        config.serial_port,
        config.serial_baud,
        config.mqtt_host,
        config.mqtt_port,
    )

    if config.serial_shared_secret == DEFAULT_SERIAL_SHARED_SECRET:
        logger.critical(
            "****************************************************************\n"
            " SECURITY CRITICAL: Using default serial shared secret!\n"
            " This device is VULNERABLE to local attacks.\n"
            " [STRICT PROVISIONING] Network services (MQTT) are BLOCKED.\n"
            " Please run 'mcubridge-rotate-credentials' IMMEDIATELY.\n"
            "****************************************************************"
        )
        # In strict mode, we force the MQTT config to disabled if secret is default
        config = msgspec.structs.replace(config, mqtt_enabled=False)
        logger.warning("STRICT MODE: MQTT transport has been DISABLED for security.")

    daemon = None
    try:
        if uvloop is None:
            raise RuntimeError("python3-uvloop is required")
        daemon = BridgeDaemon(config)

        # [SIL-2] Unified entry point via asyncio.Runner (Python 3.11+)
        # This handles signal registration and loop lifecycle deterministically.
        with asyncio.Runner(loop_factory=uvloop.new_event_loop) as runner:
            runner.run(daemon.run())

    except KeyboardInterrupt:
        logger.info("Daemon interrupted by user.")
    except (
        OSError,
        RuntimeError,
        ValueError,
        TypeError,
        asyncio.TimeoutError,
        msgspec.MsgspecError,
        tenacity.RetryError,
    ) as exc:
        logger.critical("Fatal error: %s", exc, exc_info=not isinstance(exc, RuntimeError))
        sys.exit(1)
    except BaseException as exc:
        # [SIL-2] Catch-all for unhandled system-level errors
        logger.critical("Unhandled system error: %s", exc, exc_info=True)
        sys.exit(1)
    finally:
        if daemon is not None:
            daemon.state.cleanup()


if __name__ == "__main__":
    app()
