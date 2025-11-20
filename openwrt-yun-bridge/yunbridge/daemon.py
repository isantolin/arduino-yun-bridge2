#!/usr/bin/env python3
"""Async orchestrator for the Arduino Yun Bridge v2 daemon."""
from __future__ import annotations

import asyncio
import logging
import os
import ssl
import struct
import sys
from typing import Any, Awaitable, Callable, Optional, Tuple, TypeVar, cast

import serial
import paho.mqtt.client as paho_client
from paho.mqtt.packettypes import PacketTypes
from paho.mqtt.properties import Properties
from yunbridge.rpc import protocol
from yunbridge.rpc.frame import Frame
from yunbridge.rpc.protocol import Command, Status

from tenacity import (
    AsyncRetrying,
    RetryCallState,
    retry_if_exception_type,
    stop_never,
    wait_exponential,
)

from yunbridge.common import (
    DecodeError,
    cobs_decode,
    cobs_encode,
)
from yunbridge.config.logging import configure_logging
from yunbridge.config.settings import RuntimeConfig, load_runtime_config
from yunbridge.const import MQTT_TLS_MIN_VERSION, SERIAL_TERMINATOR
from yunbridge.mqtt import (
    Client as MQTTClient,
    MQTTClientProtocol,
    MQTTError,
    QOSLevel,
    ProtocolVersion,
)
from yunbridge.services.runtime import (
    BridgeService,
    TOPIC_ANALOG,
    TOPIC_CONSOLE,
    TOPIC_DATASTORE,
    TOPIC_DIGITAL,
    TOPIC_FILE,
    TOPIC_MAILBOX,
    TOPIC_SHELL,
)
from yunbridge.state.context import RuntimeState, create_runtime_state
from yunbridge.state.status import cleanup_status_file, status_writer
from yunbridge.watchdog import WatchdogKeepalive
import serial_asyncio  # type: ignore[import]

OPEN_SERIAL_CONNECTION = cast(
    Callable[
        ...,  # type: ignore[misc]
        Awaitable[Tuple[asyncio.StreamReader, asyncio.StreamWriter]],
    ],
    cast(Any, serial_asyncio.open_serial_connection),  # type: ignore[misc]
)


logger = logging.getLogger("yunbridge")

T = TypeVar("T")


async def _serial_sender_not_ready(command_id: int, _: bytes) -> bool:
    logger.warning("Serial disconnected; dropping frame 0x%02X", command_id)
    return False


def _make_before_sleep_log(action: str) -> Callable[[RetryCallState], None]:
    def _log(retry_state: RetryCallState) -> None:
        delay = (
            retry_state.next_action.sleep
            if retry_state.next_action is not None
            else None
        )
        exc = retry_state.outcome.exception() if retry_state.outcome else None
        if delay is None:
            logger.warning("%s failed (%s); retrying soon.", action, exc)
        else:
            logger.warning(
                "%s failed (%s); retrying in %.1fs.",
                action,
                exc,
                delay,
            )

    return _log


async def _run_with_retry(
    action: str,
    handler: Callable[[], Awaitable[T]],
    *,
    base_delay: float,
    retry_exceptions: Tuple[type[BaseException], ...],
    announce_attempt: Optional[Callable[[], None]] = None,
) -> T:
    max_delay = max(base_delay, base_delay * 8)
    retryer = AsyncRetrying(
        reraise=True,
        stop=stop_never,
        retry=retry_if_exception_type(retry_exceptions),
        wait=wait_exponential(
            multiplier=base_delay,
            min=base_delay,
            max=max_delay,
        ),
        before_sleep=_make_before_sleep_log(action),
    )

    async for attempt in retryer:
        with attempt:
            if announce_attempt:
                announce_attempt()
            return await handler()

    raise RuntimeError(f"Retry loop exhausted for {action}")


async def _open_serial_connection_with_retry(
    config: RuntimeConfig,
) -> Tuple[asyncio.StreamReader, asyncio.StreamWriter]:
    base_delay = float(max(1, config.reconnect_delay))

    async def _connect() -> Tuple[asyncio.StreamReader, asyncio.StreamWriter]:
        return await OPEN_SERIAL_CONNECTION(
            url=config.serial_port,
            baudrate=config.serial_baud,
            exclusive=True,
        )

    return await _run_with_retry(
        f"Serial connection to {config.serial_port}",
        _connect,
        base_delay=base_delay,
        retry_exceptions=(
            serial.SerialException,
            ConnectionResetError,
            OSError,
        ),
        announce_attempt=lambda: logger.info(
            "Connecting to serial port %s at %d baud...",
            config.serial_port,
            config.serial_baud,
        ),
    )


async def _connect_mqtt_with_retry(
    config: RuntimeConfig,
    client: MQTTClientProtocol,
):
    base_delay = float(max(1, config.reconnect_delay))

    async def _connect() -> None:
        await client.connect()

    return await _run_with_retry(
        f"MQTT connection to {config.mqtt_host}:{config.mqtt_port}",
        _connect,
        base_delay=base_delay,
        retry_exceptions=(
            MQTTError,
            asyncio.TimeoutError,
            OSError,
        ),
        announce_attempt=lambda: logger.info(
            "Connecting to MQTT broker at %s:%d...",
            config.mqtt_host,
            config.mqtt_port,
        ),
    )


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


async def serial_reader_task(
    config: RuntimeConfig,
    state: RuntimeState,
    service: BridgeService,
) -> None:
    reconnect_delay = max(1, config.reconnect_delay)

    while True:
        reader: Optional[asyncio.StreamReader] = None
        writer: Optional[asyncio.StreamWriter] = None
        try:
            reader, writer = await _open_serial_connection_with_retry(config)

            state.serial_writer = writer

            async def _registered_sender(
                cmd: int,
                data: bytes,
                *,
                writer_ref: asyncio.StreamWriter = writer,
            ) -> bool:
                return await _send_serial_frame(state, writer_ref, cmd, data)

            service.register_serial_sender(_registered_sender)
            logger.info("Serial port connected successfully.")
            try:
                await service.on_serial_connected()
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
                            human_hex = " ".join(
                                f"{byte:02x}" for byte in appended
                            )
                            logger.debug(
                                "Decode error raw bytes (len=%d): %s",
                                len(appended),
                                human_hex,
                            )
                        truncated = encoded_packet[:32]
                        payload = struct.pack(
                            ">H", 0xFFFF
                        ) + truncated
                        try:
                            await service.send_frame(
                                Status.MALFORMED.value, payload
                            )
                        except Exception:
                            logger.exception(
                                "Failed to request MCU retransmission after "
                                "decode error"
                            )
                        continue

                    try:
                        frame = Frame.from_bytes(raw_frame)
                        await service.handle_mcu_frame(
                            frame.command_id,
                            frame.payload,
                        )
                    except ValueError as exc:
                        header_hex = raw_frame[
                            : protocol.CRC_COVERED_HEADER_SIZE
                        ].hex()
                        logger.warning(
                            (
                                "Frame parse error %s for raw %s "
                                "(len=%d header=%s)"
                            ),
                            exc,
                            raw_frame.hex(),
                            len(raw_frame),
                            header_hex,
                        )
                        status = Status.MALFORMED
                        if "crc mismatch" in str(exc).lower():
                            status = Status.CRC_MISMATCH
                        command_hint = 0xFFFF
                        if (
                            len(raw_frame)
                            >= protocol.CRC_COVERED_HEADER_SIZE
                        ):
                            _, _, command_hint = struct.unpack(
                                protocol.CRC_COVERED_HEADER_FORMAT,
                                raw_frame[
                                    : protocol.CRC_COVERED_HEADER_SIZE
                                ],
                            )
                        truncated = raw_frame[:32]
                        payload = (
                            struct.pack(">H", command_hint) + truncated
                        )
                        try:
                            await service.send_frame(status.value, payload)
                        except Exception:
                            logger.exception(
                                "Failed to notify MCU about frame parse error"
                            )
                    except Exception:
                        logger.exception(
                            "Unhandled error processing MCU frame"
                        )
                else:
                    buffer.append(byte[0])
        except (serial.SerialException, asyncio.IncompleteReadError) as exc:
            logger.error("Serial communication error: %s", exc)
        except ConnectionResetError:
            logger.error("Serial connection reset.")
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
        message_to_publish = await state.mqtt_publish_queue.get()
        topic_name = message_to_publish.topic_name
        try:
            await client.publish(
                topic_name,
                message_to_publish.payload,
                qos=int(message_to_publish.qos),
                retain=message_to_publish.retain,
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


async def _mqtt_subscriber_loop(
    client: MQTTClientProtocol,
    service: BridgeService,
) -> None:
    try:
        async with client.unfiltered_messages() as messages:
            async for message in messages:
                topic = message.topic or ""
                if not topic:
                    continue
                try:
                    payload = message.payload or b""
                    await service.handle_mqtt_message(topic, payload)
                except Exception:
                    logger.exception(
                        "Error processing MQTT topic %s",
                        topic,
                    )
    except asyncio.CancelledError:
        logger.info("MQTT subscriber loop cancelled.")
        raise
    except MQTTError as exc:
        logger.warning("MQTT subscriber loop stopped: %s", exc)
        raise


def _build_mqtt_tls_context(config: RuntimeConfig) -> Optional[ssl.SSLContext]:
    if not config.tls_enabled:
        return None

    cafile = config.mqtt_cafile
    if not cafile:
        raise RuntimeError(
            "MQTT TLS is enabled but 'mqtt_cafile' is not configured."
        )
    if not os.path.exists(cafile):
        raise FileNotFoundError(f"TLS CA file does not exist: {cafile}")

    try:
        context = ssl.create_default_context(
            ssl.Purpose.SERVER_AUTH,
            cafile=cafile,
        )
        context.minimum_version = MQTT_TLS_MIN_VERSION
        if config.mqtt_certfile and config.mqtt_keyfile:
            context.load_cert_chain(
                certfile=config.mqtt_certfile,
                keyfile=config.mqtt_keyfile,
            )
            logger.info(
                "Using MQTT TLS with client certificate authentication."
            )
        else:
            logger.info("Using MQTT TLS with CA verification only.")
        return context
    except (ssl.SSLError, FileNotFoundError) as exc:
        raise RuntimeError(f"Failed to create TLS context: {exc}") from exc


def _build_mqtt_connect_properties() -> Properties:
    props = Properties(PacketTypes.CONNECT)
    props.session_expiry_interval = 0
    props.request_response_information = 1
    props.request_problem_information = 1
    return props


async def mqtt_task(
    config: RuntimeConfig,
    state: RuntimeState,
    service: BridgeService,
    tls_context: Optional[ssl.SSLContext],
) -> None:
    reconnect_delay = max(1, config.reconnect_delay)
    prefix = state.mqtt_topic_prefix

    while True:
        client_logger = logging.getLogger("yunbridge.mqtt.client")
        client = cast(
            MQTTClientProtocol,
            MQTTClient(
                hostname=config.mqtt_host,
                port=config.mqtt_port,
                username=config.mqtt_user or None,
                password=config.mqtt_pass or None,
                tls_context=tls_context,
                logger=client_logger,
                protocol=ProtocolVersion.V5,
                clean_start=paho_client.MQTT_CLEAN_START_FIRST_ONLY,
                properties=_build_mqtt_connect_properties(),
            ),
        )
        should_retry = True
        try:
            await _connect_mqtt_with_retry(
                config,
                client,
            )
            logger.info("Connected to MQTT broker.")

            subscriptions: Tuple[Tuple[str, QOSLevel], ...] = (
                (f"{prefix}/{TOPIC_DIGITAL}/+/mode", QOSLevel.QOS_0),
                (f"{prefix}/{TOPIC_DIGITAL}/+/read", QOSLevel.QOS_0),
                (f"{prefix}/{TOPIC_DIGITAL}/+", QOSLevel.QOS_0),
                (f"{prefix}/{TOPIC_ANALOG}/+/read", QOSLevel.QOS_0),
                (f"{prefix}/{TOPIC_ANALOG}/+", QOSLevel.QOS_0),
                (f"{prefix}/{TOPIC_CONSOLE}/in", QOSLevel.QOS_0),
                (f"{prefix}/{TOPIC_DATASTORE}/put/#", QOSLevel.QOS_0),
                (f"{prefix}/{TOPIC_DATASTORE}/get/#", QOSLevel.QOS_0),
                (f"{prefix}/{TOPIC_MAILBOX}/write", QOSLevel.QOS_0),
                (f"{prefix}/{TOPIC_MAILBOX}/read", QOSLevel.QOS_0),
                (f"{prefix}/{TOPIC_SHELL}/run", QOSLevel.QOS_0),
                (f"{prefix}/{TOPIC_SHELL}/run_async", QOSLevel.QOS_0),
                (f"{prefix}/{TOPIC_SHELL}/poll/#", QOSLevel.QOS_0),
                (f"{prefix}/{TOPIC_SHELL}/kill/#", QOSLevel.QOS_0),
                (f"{prefix}/system/free_memory/get", QOSLevel.QOS_0),
                (f"{prefix}/system/version/get", QOSLevel.QOS_0),
                (f"{prefix}/{TOPIC_FILE}/write/#", QOSLevel.QOS_0),
                (f"{prefix}/{TOPIC_FILE}/read/#", QOSLevel.QOS_0),
                (f"{prefix}/{TOPIC_FILE}/remove/#", QOSLevel.QOS_0),
            )

            for topic, qos in subscriptions:
                await client.subscribe(topic, qos=int(qos))
            logger.info("Subscribed to %d MQTT topics.", len(subscriptions))

            async with asyncio.TaskGroup() as task_group:
                task_group.create_task(_mqtt_publisher_loop(client, state))
                task_group.create_task(_mqtt_subscriber_loop(client, service))

        except* MQTTError as exc_group:
            for exc in exc_group.exceptions:
                logger.error("MQTT error: %s", exc)
        except* (OSError, asyncio.TimeoutError) as exc_group:
            for exc in exc_group.exceptions:
                logger.error("MQTT connection error: %s", exc)
        except* asyncio.CancelledError:
            logger.info("MQTT task cancelled.")
            should_retry = False
            raise
        except* Exception as exc_group:
            for exc in exc_group.exceptions:
                logger.critical(
                    "Unhandled exception in mqtt_task",
                    exc_info=exc,
                )
        finally:
            try:
                await client.disconnect()
            except MQTTError:
                logger.debug("MQTT disconnect raised broker error; ignoring.")
            except Exception:
                logger.debug("Ignoring error while disconnecting MQTT client.")

            if should_retry:
                logger.warning(
                    "Waiting %d seconds before MQTT reconnect...",
                    reconnect_delay,
                )
                await asyncio.sleep(reconnect_delay)


async def main_async(config: RuntimeConfig) -> None:
    state = create_runtime_state(config)
    service = BridgeService(config, state)
    service.register_serial_sender(_serial_sender_not_ready)

    try:
        tls_context = _build_mqtt_tls_context(config)
    except Exception as exc:
        raise RuntimeError(f"TLS configuration invalid: {exc}") from exc

    try:
        async with asyncio.TaskGroup() as task_group:
            if config.watchdog_enabled:
                watchdog = WatchdogKeepalive(
                    interval=config.watchdog_interval,
                    state=state,
                )
                logger.info(
                    "Starting watchdog keepalive at %.2f second interval",
                    config.watchdog_interval,
                )
                task_group.create_task(watchdog.run())
            task_group.create_task(
                serial_reader_task(config, state, service)
            )
            task_group.create_task(
                mqtt_task(config, state, service, tls_context)
            )
            task_group.create_task(
                status_writer(state, config.status_interval)
            )
    except* asyncio.CancelledError:
        logger.info("Main task cancelled; shutting down.")
    except* Exception as exc_group:
        for exc in exc_group.exceptions:
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
        for exc in exc_group.exceptions:
            logger.critical("Fatal error in main execution", exc_info=exc)
    except Exception:
        logger.critical("Fatal error in main execution", exc_info=True)


if __name__ == "__main__":
    main()
