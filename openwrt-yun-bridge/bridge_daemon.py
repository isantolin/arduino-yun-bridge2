#!/usr/bin/env python3
"""Async orchestrator for the Arduino Yun Bridge v2 daemon."""
from __future__ import annotations

import asyncio
import logging
import os
import ssl
from typing import Optional, Tuple

import serial
from yunrpc import cobs
from yunrpc.frame import Frame
from yunrpc.protocol import Command, Status

from yunbridge.config.logging import configure_logging
from yunbridge.config.settings import RuntimeConfig, load_runtime_config
from yunbridge.mqtt import (
    AccessRefusedError,
    Client as MQTTClient,
    ConnectionCloseForcedError,
    ConnectionLostError,
    QOSLevel,
)
from yunbridge.services.runtime import (
    BridgeService,
    TOPIC_ANALOG,
    TOPIC_CONSOLE,
    TOPIC_DATASTORE,
    TOPIC_DIGITAL,
    TOPIC_FILE,
    TOPIC_MAILBOX,
    TOPIC_SH,
)
from yunbridge.state.context import RuntimeState, create_runtime_state
from yunbridge.state.status import cleanup_status_file, status_writer
from yunbridge.vendor import serial_asyncio

logger = logging.getLogger("yunbridge")

SERIAL_TERMINATOR = b"\x00"


async def _serial_sender_not_ready(command_id: int, _: bytes) -> bool:
    logger.warning("Serial disconnected; dropping frame 0x%02X", command_id)
    return False


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
        raw_frame = Frame.build(command_id, payload)
        encoded_frame = cobs.encode(raw_frame) + SERIAL_TERMINATOR
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
            logger.info(
                "Connecting to serial port %s at %d baud...",
                config.serial_port,
                config.serial_baud,
            )
            reader, writer = await serial_asyncio.open_serial_connection(
                url=config.serial_port,
                baudrate=config.serial_baud,
            )

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
                        raw_frame = cobs.decode(encoded_packet)
                        command_id, payload = Frame.parse(raw_frame)
                        await service.handle_mcu_frame(command_id, payload)
                    except ValueError as exc:
                        logger.warning(
                            "COBS/frame parsing error %s for packet %s",
                            exc,
                            encoded_packet.hex(),
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
    client: MQTTClient,
    state: RuntimeState,
) -> None:
    while True:
        topic_name = "<unknown>"
        try:
            message_to_publish = await state.mqtt_publish_queue.get()
            topic_name = message_to_publish.topic_name
            try:
                await client.publish(message_to_publish)
            finally:
                state.mqtt_publish_queue.task_done()
        except asyncio.CancelledError:
            logger.info("MQTT publisher loop cancelled.")
            raise
        except (ConnectionLostError, ConnectionCloseForcedError):
            logger.warning(
                "MQTT publish failed; broker disconnected (topic=%s)",
                topic_name,
            )
            await asyncio.sleep(1)
        except Exception:
            logger.exception(
                "Failed to publish MQTT message for topic %s",
                topic_name,
            )


async def _mqtt_subscriber_loop(
    client: MQTTClient,
    service: BridgeService,
) -> None:
    async for message in client.delivered_messages():
        if not message.topic_name:
            continue
        try:
            payload = message.payload or b""
            await service.handle_mqtt_message(message.topic_name, payload)
        except Exception:
            logger.exception(
                "Error processing MQTT topic %s",
                message.topic_name,
            )


def _build_mqtt_tls_context(config: RuntimeConfig) -> Optional[ssl.SSLContext]:
    if not config.tls_enabled:
        return None

    cafile = config.mqtt_cafile
    if not cafile or not os.path.exists(cafile):
        logger.warning("TLS enabled but CA file is missing: %s", cafile)
        return None

    try:
        context = ssl.create_default_context(
            ssl.Purpose.SERVER_AUTH,
            cafile=cafile,
        )
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
        logger.error("Failed to create TLS context: %s", exc)
        return None


async def mqtt_task(
    config: RuntimeConfig,
    state: RuntimeState,
    service: BridgeService,
    tls_context: Optional[ssl.SSLContext],
) -> None:
    reconnect_delay = max(1, config.reconnect_delay)
    prefix = state.mqtt_topic_prefix

    while True:
        client = MQTTClient(loop=asyncio.get_running_loop())
        publisher_task: Optional[asyncio.Task[None]] = None
        subscriber_task: Optional[asyncio.Task[None]] = None
        disconnect_future: Optional[asyncio.Future[Exception | None]] = None
        try:
            logger.info(
                "Connecting to MQTT broker at %s:%d...",
                config.mqtt_host,
                config.mqtt_port,
            )
            connect_result = await client.connect(
                host=config.mqtt_host,
                port=config.mqtt_port,
                username=config.mqtt_user,
                password=config.mqtt_pass,
                ssl=tls_context,
            )
            disconnect_future = connect_result.disconnect_reason
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
                (f"{prefix}/{TOPIC_SH}/run", QOSLevel.QOS_0),
                (f"{prefix}/{TOPIC_SH}/run_async", QOSLevel.QOS_0),
                (f"{prefix}/{TOPIC_SH}/poll/#", QOSLevel.QOS_0),
                (f"{prefix}/{TOPIC_SH}/kill/#", QOSLevel.QOS_0),
                (f"{prefix}/system/free_memory/get", QOSLevel.QOS_0),
                (f"{prefix}/system/version/get", QOSLevel.QOS_0),
                (f"{prefix}/{TOPIC_FILE}/write/#", QOSLevel.QOS_0),
                (f"{prefix}/{TOPIC_FILE}/read/#", QOSLevel.QOS_0),
                (f"{prefix}/{TOPIC_FILE}/remove/#", QOSLevel.QOS_0),
            )
            await client.subscribe(*subscriptions)
            logger.info("Subscribed to %d MQTT topics.", len(subscriptions))

            publisher_task = asyncio.create_task(
                _mqtt_publisher_loop(client, state)
            )
            subscriber_task = asyncio.create_task(
                _mqtt_subscriber_loop(client, service)
            )

            await asyncio.wait(
                [disconnect_future, publisher_task, subscriber_task],
                return_when=asyncio.FIRST_COMPLETED,
            )
        except AccessRefusedError:
            logger.critical("MQTT access refused; check credentials.")
        except ConnectionLostError:
            logger.error("MQTT connection lost; will retry.")
        except ConnectionCloseForcedError:
            logger.warning(
                "MQTT connection closed by broker; retrying."
            )
        except (OSError, asyncio.TimeoutError) as exc:
            logger.error("MQTT connection error: %s", exc)
        except asyncio.CancelledError:
            logger.info("MQTT task cancelled.")
            raise
        except Exception:
            logger.critical("Unhandled exception in mqtt_task", exc_info=True)
        finally:
            for task in (publisher_task, subscriber_task):
                if task and not task.done():
                    task.cancel()
            pending = [t for t in (publisher_task, subscriber_task) if t]
            if pending:
                await asyncio.gather(*pending, return_exceptions=True)
            try:
                await client.disconnect()
            except Exception:
                logger.debug("Ignoring error while disconnecting MQTT client.")

            logger.warning(
                "Waiting %d seconds before MQTT reconnect...",
                reconnect_delay,
            )
            await asyncio.sleep(reconnect_delay)


async def main_async(config: RuntimeConfig) -> None:
    state = create_runtime_state(config)
    service = BridgeService(config, state)
    service.register_serial_sender(_serial_sender_not_ready)

    tls_context = _build_mqtt_tls_context(config)

    try:
        async with asyncio.TaskGroup() as task_group:
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
    except ExceptionGroup as exc_group:
        for exc in exc_group.exceptions:
            logger.critical("Fatal error in main execution", exc_info=exc)
    except Exception:
        logger.critical("Fatal error in main execution", exc_info=True)


if __name__ == "__main__":
    main()
