#!/usr/bin/env python3
"""Async orchestrator for the Arduino Yun Bridge v2 daemon."""
from __future__ import annotations

import asyncio
import contextlib
import logging
import ssl
import struct
import sys
import time
from collections.abc import AsyncIterator
from dataclasses import dataclass
from typing import Any, Awaitable, Callable, Protocol, TypeVar, cast

from builtins import BaseExceptionGroup, ExceptionGroup

import serial
import paho.mqtt.client as paho_client
from paho.mqtt.packettypes import PacketTypes
from paho.mqtt.properties import Properties
from yunbridge.rpc import protocol
from yunbridge.rpc.frame import Frame
from yunbridge.rpc.protocol import Command, Status

from yunbridge.common import (
    DecodeError,
    cobs_decode,
    cobs_encode,
    pack_u16,
)
from yunbridge.config.logging import configure_logging
from yunbridge.config.settings import RuntimeConfig, load_runtime_config
from yunbridge.config.tls import build_tls_context, resolve_tls_material
from yunbridge.const import SERIAL_TERMINATOR
from yunbridge.mqtt import (
    MQTTClient,
    MQTTError,
    QOSLevel,
    ProtocolVersion,
    as_inbound_message,
)
from yunbridge.protocol import Topic, topic_path
from yunbridge.services.runtime import (
    BridgeService,
    SendFrameCallable,
    SerialHandshakeFatal,
)
from yunbridge.state.context import RuntimeState, create_runtime_state
from yunbridge.state.status import cleanup_status_file, status_writer
from yunbridge.watchdog import WatchdogKeepalive
from yunbridge.metrics import (
    PrometheusExporter,
    publish_bridge_snapshots,
    publish_metrics,
)
import serial_asyncio
from tenacity import (
    AsyncRetrying,
    RetryCallState,
    retry_if_exception_type,
    stop_never,
    wait_exponential,
)


async def _connect_mqtt_with_retry(
    config: RuntimeConfig,
    client: MQTTClient,
) -> None:
    """Compatibility shim retained for legacy monkeypatches.

    The modern implementation relies on the async context manager exposed by
    `aiomqtt.Client`, but older tests still patch this hook to prevent real
    broker connections. Keeping this no-op coroutine avoids breaking those
    fixtures without affecting runtime behavior.
    """

    del config  # unused in shim
    await client.connect()

OPEN_SERIAL_CONNECTION: Callable[
    ..., Awaitable[tuple[asyncio.StreamReader, asyncio.StreamWriter]]
] = serial_asyncio.open_serial_connection


logger = logging.getLogger("yunbridge")

T = TypeVar("T")


class MQTTClientProtocol(Protocol):
    async def publish(
        self,
        topic: str,
        payload: bytes | bytearray | memoryview,
        *,
        qos: int = 0,
        retain: bool = False,
        properties: Any | None = None,
    ) -> Any:
        ...

    async def subscribe(self, topic: str, qos: int = 0) -> Any:
        ...

    def unfiltered_messages(
        self,
    ) -> contextlib.AbstractAsyncContextManager[AsyncIterator[Any]]:
        ...

    async def disconnect(self) -> None:
        ...


MAX_SERIAL_PACKET_BYTES = (
    protocol.CRC_COVERED_HEADER_SIZE
    + protocol.MAX_PAYLOAD_SIZE
    + protocol.CRC_SIZE
    + 4
)


@dataclass(slots=True)
class _SupervisedTaskSpec:
    name: str
    factory: Callable[[], Awaitable[None]]
    fatal_exceptions: tuple[type[BaseException], ...] = ()
    max_restarts: int | None = None
    restart_interval: float = 60.0
    min_backoff: float = 1.0
    max_backoff: float = 30.0


@dataclass(slots=True)
class _RetryPolicy:
    action: str
    retry_exceptions: tuple[type[BaseException], ...]
    base_delay: float
    max_delay: float
    announce_attempt: Callable[[], None] | None = None


class _RetryableSupervisorError(Exception):
    """Sentinel exception to request another supervisor attempt."""

    def __init__(
        self,
        original: BaseException,
        *,
        reset_backoff: bool,
    ) -> None:
        super().__init__(str(original))
        self.original = original
        self.reset_backoff = reset_backoff


def _unwrap_retryable_exception_group(
    group: BaseExceptionGroup[BaseException],
    retry_types: tuple[type[BaseException], ...],
) -> BaseException | None:
    collected: list[BaseException] = []

    def _collect(
        exc: BaseException | BaseExceptionGroup[BaseException],
    ) -> bool:
        if isinstance(exc, BaseExceptionGroup):
            members = cast(
                tuple[BaseException, ...],
                cast(Any, exc).exceptions,
            )
            return all(_collect(inner) for inner in members)
        if isinstance(exc, retry_types):
            collected.append(exc)
            return True
        return False

    if _collect(group) and collected:
        return collected[0]
    return None


class _SupervisorWait:
    """Stateful wait strategy that allows backoff resets."""

    def __init__(self, *, min_delay: float, max_delay: float) -> None:
        self._min = max(0.1, min_delay)
        self._max = max(self._min, max_delay)
        self._streak = 0

    def __call__(self, retry_state: RetryCallState) -> float:
        outcome = retry_state.outcome
        reset_requested = False
        if outcome is not None and outcome.failed:
            exc = outcome.exception()
            if isinstance(exc, _RetryableSupervisorError):
                reset_requested = exc.reset_backoff

        if reset_requested or self._streak <= 0:
            self._streak = 1
        else:
            self._streak += 1

        delay = min(self._max, self._min * (2 ** (self._streak - 1)))
        return delay


async def _serial_sender_not_ready(command_id: int, _: bytes) -> bool:
    logger.warning(
        "Serial disconnected; dropping frame 0x%02X",
        command_id,
    )
    return False


async def _run_with_retry(
    policy: _RetryPolicy,
    handler: Callable[[], Awaitable[T]],
) -> T:
    """Retry *handler* indefinitely according to *policy*."""

    if not policy.retry_exceptions:
        raise ValueError("retry_exceptions must not be empty")

    base_delay = max(0.1, policy.base_delay)
    max_delay = max(base_delay, policy.max_delay)

    def _before_sleep(retry_state: RetryCallState) -> None:
        exc: BaseException | None = None
        outcome: Any = retry_state.outcome
        if outcome is not None:
            exc = outcome.exception()

        next_action: Any = retry_state.next_action
        sleep_for: float
        if (
            next_action is not None
            and getattr(next_action, "sleep", None) is not None
        ):
            sleep_for = float(next_action.sleep)
        else:
            sleep_for = base_delay
        logger.warning(
            "%s failed (%s); retrying in %.1fs.",
            policy.action,
            exc,
            sleep_for,
        )

    retry_kwargs: dict[str, Any] = {
        "retry": retry_if_exception_type(policy.retry_exceptions),
        "wait": wait_exponential(
            multiplier=base_delay,
            min=base_delay,
            max=max_delay,
        ),
        "stop": stop_never,
        "reraise": True,
        "before_sleep": _before_sleep,
    }

    if policy.announce_attempt is not None:
        announce_callback = policy.announce_attempt

        def _before(_: RetryCallState) -> None:
            announce_callback()

        retry_kwargs["before"] = _before

    retryer = AsyncRetrying(**retry_kwargs)

    async def _invoke_handler() -> T:
        try:
            return await handler()
        except BaseExceptionGroup as exc_group:
            flattened = _unwrap_retryable_exception_group(
                exc_group,
                policy.retry_exceptions,
            )
            if flattened is not None:
                raise flattened from exc_group
            raise

    try:
        async for attempt in retryer:
            with attempt:
                return await _invoke_handler()
    except asyncio.CancelledError:
        logger.debug("%s retry loop cancelled", policy.action)
        raise

    raise RuntimeError(f"{policy.action} retry loop terminated unexpectedly")


async def _supervise_task(
    name: str,
    coro_factory: Callable[[], Awaitable[None]],
    *,
    fatal_exceptions: tuple[type[BaseException], ...] = (),
    min_backoff: float = 1.0,
    max_backoff: float = 30.0,
    state: RuntimeState | None = None,
    max_restarts: int | None = None,
    restart_interval: float = 60.0,
) -> None:
    """Run *coro_factory* restarting it on failures."""

    restart_window = max(1.0, restart_interval)
    restarts_in_window = 0
    window_started = time.monotonic()

    wait_strategy = _SupervisorWait(
        min_delay=min_backoff,
        max_delay=max_backoff,
    )

    def _before_sleep(retry_state: RetryCallState) -> None:
        outcome: Any = retry_state.outcome
        next_action: Any = retry_state.next_action
        if outcome is None or not getattr(outcome, "failed", False):
            return
        exc = outcome.exception()
        if not isinstance(exc, _RetryableSupervisorError):
            return
        sleep_value = None
        if next_action is not None:
            sleep_value = getattr(next_action, "sleep", None)
        delay = (
            float(sleep_value)
            if sleep_value is not None
            else max(0.1, min_backoff)
        )
        logger.warning(
            "%s task crashed; restarting in %.1fs",
            name,
            delay,
        )
        if state is not None:
            state.record_supervisor_failure(
                name,
                backoff=delay,
                exc=exc.original,
            )

    retryer = AsyncRetrying(
        retry=retry_if_exception_type(_RetryableSupervisorError),
        wait=wait_strategy,
        stop=stop_never,
        reraise=True,
        before_sleep=_before_sleep,
    )

    async for attempt in retryer:
        with attempt:
            started = time.monotonic()
            try:
                await coro_factory()
                logger.warning(
                    "%s task exited cleanly; supervisor exiting",
                    name,
                )
                if state is not None:
                    state.mark_supervisor_healthy(name)
                return
            except asyncio.CancelledError:
                logger.debug("%s supervisor cancelled", name)
                raise
            except fatal_exceptions as exc:
                logger.critical("%s task hit fatal error: %s", name, exc)
                if state is not None:
                    state.record_supervisor_failure(
                        name,
                        backoff=0.0,
                        exc=exc,
                        fatal=True,
                    )
                raise
            except Exception as exc:
                logger.exception("%s task crashed", name)

                restarts_in_window += 1
                now = time.monotonic()
                window_age = now - window_started
                if window_age > restart_window:
                    window_started = now
                    restarts_in_window = 1
                    window_age = 0.0

                if (
                    max_restarts is not None
                    and max_restarts > 0
                    and restarts_in_window > max_restarts
                ):
                    logger.critical(
                        "%s task exceeded %d restarts within %.1fs; aborting",
                        name,
                        max_restarts,
                        window_age,
                    )
                    if state is not None:
                        state.record_supervisor_failure(
                            name,
                            backoff=0.0,
                            exc=exc,
                            fatal=True,
                        )
                    raise

                elapsed = now - started
                reset_backoff = elapsed > max_backoff
                raise _RetryableSupervisorError(
                    exc,
                    reset_backoff=reset_backoff,
                ) from exc


async def _open_serial_connection_with_retry(
    config: RuntimeConfig,
) -> tuple[asyncio.StreamReader, asyncio.StreamWriter]:
    base_delay = float(max(1, config.reconnect_delay))

    policy = _RetryPolicy(
        action=f"Serial connection to {config.serial_port}",
        retry_exceptions=(
            serial.SerialException,
            ConnectionResetError,
            OSError,
        ),
        base_delay=base_delay,
        max_delay=base_delay * 8,
        announce_attempt=lambda: logger.info(
            "Connecting to serial port %s at %d baud...",
            config.serial_port,
            config.serial_baud,
        ),
    )

    async def _connect() -> tuple[asyncio.StreamReader, asyncio.StreamWriter]:
        return await OPEN_SERIAL_CONNECTION(
            url=config.serial_port,
            baudrate=config.serial_baud,
            exclusive=True,
        )

    return await _run_with_retry(policy, _connect)


async def _send_serial_frame(
    state: RuntimeState,
    writer: asyncio.StreamWriter,
    command_id: int,
    payload: bytes,
) -> bool:
    if writer.is_closing():
        logger.error(
            "Serial writer closed; cannot send frame 0x%02X",
            command_id,
        )
        return False

    try:
        raw_frame = Frame(command_id, payload).to_bytes()
        encoded_frame = cobs_encode(raw_frame) + SERIAL_TERMINATOR
        writer.write(encoded_frame)
        await writer.drain()

        try:
            command_name = Command(command_id).name
        except ValueError:
            try:
                command_name = Status(command_id).name
            except ValueError:
                command_name = f"UNKNOWN_CMD_ID(0x{command_id:02X})"
        logger.debug("LINUX > %s payload=%s", command_name, payload.hex())
        return True
    except ValueError as exc:
        logger.error(
            "Refusing to send frame 0x%02X: %s", command_id, exc
        )
        return False
    except ConnectionResetError:
        logger.error(
            "Serial connection reset while sending frame 0x%02X",
            command_id,
        )
        if state.serial_writer and not state.serial_writer.is_closing():
            try:
                state.serial_writer.close()
                await state.serial_writer.wait_closed()
            except Exception:
                logger.exception("Error closing serial writer after reset.")
        state.serial_writer = None
        return False
    except Exception:
        logger.exception("Unexpected error sending frame 0x%02X", command_id)
        return False


async def _process_serial_packet(
    encoded_packet: bytes,
    service: BridgeService,
    state: RuntimeState,
) -> None:
    try:
        raw_frame = cobs_decode(encoded_packet)
    except DecodeError as exc:
        packet_hex = encoded_packet.hex()
        logger.warning(
            "COBS decode error %s for packet %s (len=%d)",
            exc,
            packet_hex,
            len(encoded_packet),
        )
        if logger.isEnabledFor(logging.DEBUG):
            appended = encoded_packet + SERIAL_TERMINATOR
            human_hex = " ".join(f"{byte:02x}" for byte in appended)
            logger.debug(
                "Decode error raw bytes (len=%d): %s",
                len(appended),
                human_hex,
            )
        state.record_serial_decode_error()
        truncated = encoded_packet[:32]
        payload = pack_u16(0xFFFF) + truncated
        try:
            await service.send_frame(Status.MALFORMED.value, payload)
        except Exception:  # pragma: no cover - defensive logging
            logger.exception(
                "Failed to request MCU retransmission after decode error"
            )
        return

    try:
        frame = Frame.from_bytes(raw_frame)
    except ValueError as exc:
        header_hex = raw_frame[: protocol.CRC_COVERED_HEADER_SIZE].hex()
        logger.warning(
            (
                "Frame parse error %s for raw %s (len=%d header=%s)"
            ),
            exc,
            raw_frame.hex(),
            len(raw_frame),
            header_hex,
        )
        status = Status.MALFORMED
        if "crc mismatch" in str(exc).lower():
            status = Status.CRC_MISMATCH
            state.record_serial_crc_error()
        command_hint = 0xFFFF
        if len(raw_frame) >= protocol.CRC_COVERED_HEADER_SIZE:
            _, _, command_hint = struct.unpack(
                protocol.CRC_COVERED_HEADER_FORMAT,
                raw_frame[: protocol.CRC_COVERED_HEADER_SIZE],
            )
        truncated = raw_frame[:32]
        payload = pack_u16(command_hint) + truncated
        try:
            await service.send_frame(status.value, payload)
        except Exception:  # pragma: no cover - defensive logging
            logger.exception(
                "Failed to notify MCU about frame parse error"
            )
        return
    except Exception:
        logger.exception("Unhandled error processing MCU frame")
        return

    try:
        await service.handle_mcu_frame(frame.command_id, frame.payload)
    except Exception:
        logger.exception("Unhandled error processing MCU frame")


async def serial_reader_task(
    config: RuntimeConfig,
    state: RuntimeState,
    service: BridgeService,
) -> None:
    reconnect_delay = max(1, config.reconnect_delay)

    while True:
        reader: asyncio.StreamReader | None = None
        writer: asyncio.StreamWriter | None = None
        should_retry = True
        try:
            reader, writer = await _open_serial_connection_with_retry(config)

            state.serial_writer = writer

            previous_sender: SendFrameCallable | None = getattr(
                service, "_serial_sender", None
            )

            async def _registered_sender(
                cmd: int,
                data: bytes,
                *,
                writer_ref: asyncio.StreamWriter = writer,
            ) -> bool:
                return await _send_serial_frame(state, writer_ref, cmd, data)

            if previous_sender is not None:
                prev_sender: SendFrameCallable = previous_sender

                async def _chained_sender(
                    cmd: int,
                    data: bytes,
                    *,
                    prior_sender: SendFrameCallable = prev_sender,
                ) -> bool:
                    try:
                        await prior_sender(cmd, data)
                    except Exception:  # pragma: no cover - defensive
                        logger.exception(
                            "Serial sender hook raised an exception"
                        )
                    return await _registered_sender(cmd, data)

                service.register_serial_sender(_chained_sender)
            else:
                service.register_serial_sender(_registered_sender)
            logger.info("Serial port connected successfully.")
            try:
                await service.on_serial_connected()
            except SerialHandshakeFatal as exc:
                should_retry = False
                logger.critical("%s", exc)
                raise
            except Exception:
                logger.exception(
                    "Error running post-connect hooks for serial link"
                )

            buffer = bytearray()
            while True:
                byte = await reader.read(1)
                if not byte:
                    logger.warning("Serial stream ended; reconnecting.")
                    break

                if byte == SERIAL_TERMINATOR:
                    if not buffer:
                        continue
                    encoded_packet = bytes(buffer)
                    buffer.clear()
                    await _process_serial_packet(
                        encoded_packet,
                        service,
                        state,
                    )
                else:
                    buffer.append(byte[0])
                    if len(buffer) > MAX_SERIAL_PACKET_BYTES:
                        snapshot = bytes(buffer[:32])
                        buffer.clear()
                        state.record_serial_decode_error()
                        logger.warning(
                            "Serial packet exceeded %d bytes; "
                            "requesting retransmit.",
                            MAX_SERIAL_PACKET_BYTES,
                        )
                        payload = pack_u16(0xFFFF) + snapshot
                        try:
                            await service.send_frame(
                                Status.MALFORMED.value,
                                payload,
                            )
                        except Exception:
                            logger.exception(
                                "Failed to notify MCU about oversized "
                                "serial packet"
                            )
        except (serial.SerialException, asyncio.IncompleteReadError) as exc:
            logger.error("Serial communication error: %s", exc)
        except ConnectionResetError:
            logger.error("Serial connection reset.")
        except SerialHandshakeFatal:
            raise
        except asyncio.CancelledError:
            logger.info("Serial reader task cancelled.")
            raise
        except Exception:
            logger.critical(
                "Unhandled exception in serial_reader_task",
                exc_info=True,
            )
        finally:
            if writer and not writer.is_closing():
                try:
                    writer.close()
                    await writer.wait_closed()
                except Exception:
                    logger.exception(
                        "Error closing serial writer during cleanup."
                    )
            state.serial_writer = None
            try:
                await service.on_serial_disconnected()
            except Exception:
                logger.exception(
                    "Error resetting service state after serial disconnect"
                )
            service.register_serial_sender(_serial_sender_not_ready)
            if should_retry:
                logger.warning(
                    "Serial port disconnected. Retrying in %d seconds...",
                    reconnect_delay,
                )
                await asyncio.sleep(reconnect_delay)


async def _mqtt_publisher_loop(
    client: MQTTClientProtocol,
    state: RuntimeState,
) -> None:
    while True:
        await state.flush_mqtt_spool()
        message_to_publish = await state.mqtt_publish_queue.get()
        topic_name = message_to_publish.topic_name
        try:
            await client.publish(
                topic_name,
                message_to_publish.payload,
                qos=int(message_to_publish.qos),
                retain=message_to_publish.retain,
                properties=message_to_publish.build_properties(),
            )
        except asyncio.CancelledError:
            logger.info("MQTT publisher loop cancelled.")
            try:
                state.mqtt_publish_queue.put_nowait(message_to_publish)
            except asyncio.QueueFull:
                logger.debug(
                    "MQTT publish queue full while cancelling; dropping %s",
                    topic_name,
                )
            raise
        except MQTTError as exc:
            logger.warning(
                "MQTT publish failed for %s; broker unavailable (%s)",
                topic_name,
                exc,
            )
            try:
                state.mqtt_publish_queue.put_nowait(message_to_publish)
            except asyncio.QueueFull:
                logger.error(
                    "MQTT publish queue full; dropping message for %s",
                    topic_name,
                )
            raise
        except Exception:
            logger.exception(
                "Failed to publish MQTT message for topic %s",
                topic_name,
            )
            raise
        finally:
            state.mqtt_publish_queue.task_done()
            await state.flush_mqtt_spool()


async def _mqtt_subscriber_loop(
    client: MQTTClientProtocol,
    service: BridgeService,
) -> None:
    try:
        async with client.unfiltered_messages() as stream:
            async for message in stream:
                inbound = as_inbound_message(message)
                if not inbound.topic_name:
                    continue
                try:
                    await service.handle_mqtt_message(inbound)
                except Exception:
                    logger.exception(
                        "Error processing MQTT topic %s",
                        inbound.topic_name,
                    )
    except asyncio.CancelledError:
        logger.info("MQTT subscriber loop cancelled.")
        raise
    except MQTTError as exc:
        logger.warning("MQTT subscriber loop stopped: %s", exc)
        raise


def _build_mqtt_tls_context(config: RuntimeConfig) -> ssl.SSLContext | None:
    if not config.tls_enabled:
        return None

    try:
        material = resolve_tls_material(config)
        context = build_tls_context(material)
        if material.certfile and material.keyfile:
            logger.info(
                "Using MQTT TLS with client certificate authentication."
            )
        else:
            logger.info("Using MQTT TLS with CA verification only.")
        return context
    except (ssl.SSLError, FileNotFoundError, RuntimeError) as exc:
        message = f"Failed to create TLS context: {exc}"
        raise RuntimeError(message) from exc


def _set_mqtt_property(props: Properties, camel_name: str, value: int) -> None:
    try:
        setattr(props, camel_name, value)
    except AttributeError as exc:
        # pragma: no cover - defensive: depends on paho version
        raise RuntimeError(
            f"paho-mqtt missing MQTT v5 property '{camel_name}'"
        ) from exc


def _build_mqtt_connect_properties() -> Properties:
    props = Properties(PacketTypes.CONNECT)
    _set_mqtt_property(props, "SessionExpiryInterval", 0)
    _set_mqtt_property(props, "RequestResponseInformation", 1)
    _set_mqtt_property(props, "RequestProblemInformation", 1)
    return props


async def mqtt_task(
    config: RuntimeConfig,
    state: RuntimeState,
    service: BridgeService,
    tls_context: ssl.SSLContext | None,
) -> None:
    reconnect_delay = max(1, config.reconnect_delay)
    prefix = state.mqtt_topic_prefix
    base_client_kwargs: dict[str, Any] = {
        "hostname": config.mqtt_host,
        "port": config.mqtt_port,
        "username": config.mqtt_user or None,
        "password": config.mqtt_pass or None,
        "tls_context": tls_context,
        "logger": logging.getLogger("yunbridge.mqtt.client"),
        "protocol": ProtocolVersion.V5,
        "clean_start": paho_client.MQTT_CLEAN_START_FIRST_ONLY,
    }

    while True:
        client_protocol: MQTTClientProtocol | None = None
        try:
            client_kwargs = dict(base_client_kwargs)
            client_kwargs["properties"] = _build_mqtt_connect_properties()
            raw_client = MQTTClient(**client_kwargs)
            await _connect_mqtt_with_retry(config, raw_client)
            logger.info("Connected to MQTT broker.")

            # Cast client to Protocol to satisfy static type checkers.
            client_protocol = cast(MQTTClientProtocol, raw_client)

            def _sub(
                top_segment: Topic | str,
                *segments: str,
            ) -> tuple[str, QOSLevel]:
                return (
                    topic_path(prefix, top_segment, *segments),
                    QOSLevel.QOS_0,
                )

            subscriptions: tuple[tuple[str, QOSLevel], ...] = (
                _sub(Topic.DIGITAL, "+", "mode"),
                _sub(Topic.DIGITAL, "+", "read"),
                _sub(Topic.DIGITAL, "+"),
                _sub(Topic.ANALOG, "+", "read"),
                _sub(Topic.ANALOG, "+"),
                _sub(Topic.CONSOLE, "in"),
                _sub(Topic.DATASTORE, "put", "#"),
                _sub(Topic.DATASTORE, "get", "#"),
                _sub(Topic.MAILBOX, "write"),
                _sub(Topic.MAILBOX, "read"),
                _sub(Topic.SHELL, "run"),
                _sub(Topic.SHELL, "run_async"),
                _sub(Topic.SHELL, "poll", "#"),
                _sub(Topic.SHELL, "kill", "#"),
                _sub(Topic.SYSTEM, "free_memory", "get"),
                _sub(Topic.SYSTEM, "version", "get"),
                _sub(Topic.FILE, "write", "#"),
                _sub(Topic.FILE, "read", "#"),
                _sub(Topic.FILE, "remove", "#"),
            )

            for topic, qos in subscriptions:
                await client_protocol.subscribe(topic, qos=int(qos))
            logger.info(
                "Subscribed to %d MQTT topics.",
                len(subscriptions),
            )

            async with asyncio.TaskGroup() as task_group:
                task_group.create_task(
                    _mqtt_publisher_loop(client_protocol, state)
                )
                task_group.create_task(
                    _mqtt_subscriber_loop(client_protocol, service)
                )

        except* MQTTError as exc_group:
            for exc in exc_group.exceptions:
                logger.error("MQTT error: %s", exc)
        except* (OSError, asyncio.TimeoutError) as exc_group:
            for exc in exc_group.exceptions:
                logger.error("MQTT connection error: %s", exc)
        except* asyncio.CancelledError:
            logger.info("MQTT task cancelled.")
            raise
        except* Exception as exc_group:
            for exc in exc_group.exceptions:
                logger.critical(
                    "Unhandled exception in mqtt_task",
                    exc_info=exc,
                )
        finally:
            if client_protocol is not None:
                with contextlib.suppress(Exception):
                    await client_protocol.disconnect()

        # Reconnection delay logic outside the context manager
        logger.warning(
            "Waiting %d seconds before MQTT reconnect...",
            reconnect_delay,
        )
        try:
            await asyncio.sleep(reconnect_delay)
        except asyncio.CancelledError:
            logger.info("MQTT task cancelled during backoff.")
            raise


async def main_async(config: RuntimeConfig) -> None:
    state = create_runtime_state(config)
    service = BridgeService(config, state)
    service.register_serial_sender(_serial_sender_not_ready)

    try:
        tls_context = _build_mqtt_tls_context(config)
    except Exception as exc:
        raise RuntimeError(f"TLS configuration invalid: {exc}") from exc

    async def _serial_runner() -> None:
        await serial_reader_task(config, state, service)

    async def _mqtt_runner() -> None:
        await mqtt_task(config, state, service, tls_context)

    async def _status_runner() -> None:
        await status_writer(state, config.status_interval)

    async def _metrics_runner() -> None:
        await publish_metrics(
            state,
            service.enqueue_mqtt,
            float(config.status_interval),
        )

    async def _bridge_snapshots_runner() -> None:
        await publish_bridge_snapshots(
            state,
            service.enqueue_mqtt,
            summary_interval=float(config.bridge_summary_interval),
            handshake_interval=float(config.bridge_handshake_interval),
        )

    supervised_tasks: list[_SupervisedTaskSpec] = [
        _SupervisedTaskSpec(
            name="serial-link",
            factory=_serial_runner,
            fatal_exceptions=(SerialHandshakeFatal,),
        ),
        _SupervisedTaskSpec(
            name="mqtt-link",
            factory=_mqtt_runner,
        ),
        _SupervisedTaskSpec(
            name="status-writer",
            factory=_status_runner,
            max_restarts=5,
            restart_interval=120.0,
            max_backoff=10.0,
        ),
        _SupervisedTaskSpec(
            name="metrics-publisher",
            factory=_metrics_runner,
            max_restarts=5,
            restart_interval=120.0,
            max_backoff=10.0,
        ),
    ]

    if (
        config.bridge_summary_interval > 0.0
        or config.bridge_handshake_interval > 0.0
    ):
        supervised_tasks.append(
            _SupervisedTaskSpec(
                name="bridge-snapshots",
                factory=_bridge_snapshots_runner,
                max_restarts=5,
                restart_interval=120.0,
                max_backoff=10.0,
            )
        )

    if config.watchdog_enabled:
        watchdog = WatchdogKeepalive(
            interval=config.watchdog_interval,
            state=state,
        )
        logger.info(
            "Starting watchdog keepalive at %.2f second interval",
            config.watchdog_interval,
        )
        supervised_tasks.append(
            _SupervisedTaskSpec(
                name="watchdog",
                factory=watchdog.run,
                max_restarts=5,
                restart_interval=120.0,
                max_backoff=10.0,
            )
        )

    exporter: PrometheusExporter | None = None
    if config.metrics_enabled:
        exporter = PrometheusExporter(
            state,
            config.metrics_host,
            config.metrics_port,
        )
        supervised_tasks.append(
            _SupervisedTaskSpec(
                name="prometheus-exporter",
                factory=exporter.run,
                max_restarts=5,
                restart_interval=300.0,
            )
        )

    try:
        async with asyncio.TaskGroup() as task_group:
            for spec in supervised_tasks:
                task_group.create_task(
                    _supervise_task(
                        spec.name,
                        spec.factory,
                        fatal_exceptions=spec.fatal_exceptions,
                        min_backoff=spec.min_backoff,
                        max_backoff=spec.max_backoff,
                        state=state,
                        max_restarts=spec.max_restarts,
                        restart_interval=spec.restart_interval,
                    )
                )
    except* asyncio.CancelledError:
        logger.info("Main task cancelled; shutting down.")
    except* Exception as exc_group:
        group_exc = cast(BaseExceptionGroup[BaseException], exc_group)
        for exc in getattr(group_exc, "exceptions", ()):  # pragma: no branch
            logger.critical(
                "Unhandled exception in main task group",
                exc_info=exc,
            )
        raise
    finally:
        cleanup_status_file()
        logger.info("Yun Bridge daemon stopped.")


def main() -> None:
    config = load_runtime_config()
    configure_logging(config)

    logger.info(
        "Starting Yun Bridge daemon. Serial: %s@%d MQTT: %s:%d",
        config.serial_port,
        config.serial_baud,
        config.mqtt_host,
        config.mqtt_port,
    )

    try:
        asyncio.run(main_async(config))
    except KeyboardInterrupt:
        logger.info("Daemon interrupted by user.")
    except RuntimeError as exc:
        logger.critical("Startup aborted: %s", exc)
        sys.exit(1)
    except ExceptionGroup as exc_group:
        typed_exc_group = cast(BaseExceptionGroup[BaseException], exc_group)
        for exc in typed_exc_group.exceptions:
            logger.critical("Fatal error in main execution", exc_info=exc)
    except Exception:
        logger.critical("Fatal error in main execution", exc_info=True)


if __name__ == "__main__":
    main()
