"""Serial transport helpers for the Yun Bridge daemon."""
from __future__ import annotations

import asyncio
import logging
import struct
from builtins import BaseExceptionGroup
from typing import Any

import serial
import serial_asyncio
from cobs import cobs
from tenacity import (
    AsyncRetrying,
    RetryCallState,
    retry_if_exception_type,
    stop_never,
    wait_exponential,
)

from yunbridge.common import pack_u16
from yunbridge.config.settings import RuntimeConfig
from yunbridge.const import SERIAL_TERMINATOR
from yunbridge.rpc import protocol
from yunbridge.rpc.frame import Frame
from yunbridge.rpc.protocol import Command, Status
from yunbridge.services.runtime import (
    BridgeService,
    SendFrameCallable,
    SerialHandshakeFatal,
)
from yunbridge.state.context import RuntimeState

logger = logging.getLogger("yunbridge")

OPEN_SERIAL_CONNECTION = serial_asyncio.open_serial_connection

MAX_SERIAL_PACKET_BYTES = (
    protocol.CRC_COVERED_HEADER_SIZE
    + protocol.MAX_PAYLOAD_SIZE
    + protocol.CRC_SIZE
    + 4
)


def _unwrap_retryable_exception_group(
    group: BaseExceptionGroup[BaseException],
    retry_types: tuple[type[BaseException], ...],
) -> BaseException | None:
    collected: list[BaseException] = []

    def _collect(
        exc: BaseException | BaseExceptionGroup[BaseException],
    ) -> bool:
        if isinstance(exc, BaseExceptionGroup):
            members = tuple(exc.exceptions)  # type: ignore[attr-defined]
            return all(_collect(inner) for inner in members)
        if isinstance(exc, retry_types):
            collected.append(exc)
            return True
        return False

    if _collect(group) and collected:
        return collected[0]
    return None


async def serial_sender_not_ready(command_id: int, _: bytes) -> bool:
    logger.warning(
        "Serial disconnected; dropping frame 0x%02X",
        command_id,
    )
    return False


async def _open_serial_connection_with_retry(
    config: RuntimeConfig,
) -> tuple[asyncio.StreamReader, asyncio.StreamWriter]:
    base_delay = float(max(1, config.reconnect_delay))
    max_delay = base_delay * 8
    retry_types: tuple[type[BaseException], ...] = (
        serial.SerialException,
        ConnectionResetError,
        OSError,
    )
    action = f"Serial connection to {config.serial_port}"

    def _before(_: RetryCallState) -> None:
        logger.info(
            "Connecting to serial port %s at %d baud...",
            config.serial_port,
            config.serial_baud,
        )

    def _before_sleep(retry_state: RetryCallState) -> None:
        exc: BaseException | None = None
        outcome = retry_state.outcome
        if outcome is not None:
            exc = outcome.exception()

        next_action: Any = retry_state.next_action
        sleep_for = base_delay
        if (
            next_action is not None
            and getattr(next_action, "sleep", None) is not None
        ):
            sleep_for = float(next_action.sleep)

        logger.warning(
            "%s failed (%s); retrying in %.1fs.",
            action,
            exc,
            sleep_for,
        )

    retryer = AsyncRetrying(
        retry=retry_if_exception_type(retry_types),
        wait=wait_exponential(
            multiplier=base_delay,
            min=base_delay,
            max=max_delay,
        ),
        stop=stop_never,
        reraise=True,
        before=_before,
        before_sleep=_before_sleep,
    )

    async def _connect_once() -> tuple[asyncio.StreamReader, asyncio.StreamWriter]:
        try:
            return await OPEN_SERIAL_CONNECTION(
                url=config.serial_port,
                baudrate=config.serial_baud,
                exclusive=True,
            )
        except BaseExceptionGroup as exc_group:
            flattened = _unwrap_retryable_exception_group(
                exc_group,
                retry_types,
            )
            if flattened is not None:
                raise flattened from exc_group
            raise

    try:
        async for attempt in retryer:
            with attempt:
                return await _connect_once()
    except asyncio.CancelledError:
        logger.debug("%s retry loop cancelled", action)
        raise

    raise RuntimeError(f"{action} retry loop terminated unexpectedly")


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
        raw_frame = cobs.decode(encoded_packet)
    except cobs.DecodeError as exc:
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
            service.register_serial_sender(serial_sender_not_ready)
            if should_retry:
                logger.warning(
                    "Serial port disconnected. Retrying in %d seconds...",
                    reconnect_delay,
                )
                await asyncio.sleep(reconnect_delay)


__all__ = [
    "MAX_SERIAL_PACKET_BYTES",
    "serial_reader_task",
    "serial_sender_not_ready",
    "_open_serial_connection_with_retry",
]
