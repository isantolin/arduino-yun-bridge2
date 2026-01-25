"""Serial transport helpers for the MCU Bridge daemon (Python 3.13+ Compatible).

This module uses pyserial-asyncio-fast for zero-overhead async I/O.
"""

from __future__ import annotations

import asyncio
import inspect
import logging
import struct
import time
from typing import Final, Sized, TypeGuard, cast, TYPE_CHECKING

from cobs import cobs
from mcubridge.rpc import rle
import serial_asyncio_fast  # type: ignore

from mcubridge.config.settings import RuntimeConfig
from mcubridge.const import SERIAL_BAUDRATE_NEGOTIATION_TIMEOUT
from mcubridge.rpc.protocol import FRAME_DELIMITER
from mcubridge.rpc import protocol
from mcubridge.rpc.frame import Frame
from mcubridge.rpc.protocol import Command, Status

# Import directly from handshake to avoid circular dependency via runtime
from mcubridge.services.handshake import SerialHandshakeFatal

if TYPE_CHECKING:
    from mcubridge.services.runtime import BridgeService

from mcubridge.state.context import RuntimeState

logger = logging.getLogger("mcubridge")


def format_hexdump(data: bytes, prefix: str = "") -> str:
    """Format binary data as canonical hexdump for SIL-2 compliant logging.

    Produces output in the standard hexdump format:
    XX XX XX XX  XX XX XX XX  XX XX XX XX  XX XX XX XX  |................|

    Args:
        data: Binary data to format
        prefix: Optional prefix for each line (e.g., "  ")

    Returns:
        Multi-line string with hexdump format
    """
    if not data:
        return f"{prefix}<empty>"

    lines: list[str] = []
    for offset in range(0, len(data), 16):
        chunk = data[offset: offset + 16]
        # Hex part: groups of 4 bytes separated by double space
        hex_parts: list[str] = []
        for i in range(0, 16, 4):
            group = chunk[i: i + 4]
            hex_parts.append(" ".join(f"{b:02X}" for b in group))
        hex_str = "  ".join(hex_parts)
        # ASCII part: printable chars or '.'
        ascii_str = "".join(chr(b) if 32 <= b < 127 else "." for b in chunk)
        # Pad hex_str to fixed width (47 chars for 16 bytes)
        hex_str = hex_str.ljust(47)
        lines.append(f"{prefix}{offset:04X}  {hex_str}  |{ascii_str}|")

    return "\n".join(lines)


# Explicit framing overhead: 1 code byte + 1 delimiter + ~1 byte/254 overhead + safety margin
FRAMING_OVERHEAD: Final[int] = 4

MAX_SERIAL_PACKET_BYTES = (
    protocol.CRC_COVERED_HEADER_SIZE
    + protocol.MAX_PAYLOAD_SIZE
    + protocol.CRC_SIZE
    + FRAMING_OVERHEAD
)

BinaryPacket = bytes | bytearray | memoryview


def _is_binary_packet(candidate: object) -> TypeGuard[BinaryPacket]:
    if not isinstance(candidate, (bytes, bytearray, memoryview)):
        return False
    length = len(cast(Sized, candidate))
    if length == 0:
        return False
    return length <= MAX_SERIAL_PACKET_BYTES


def _coerce_packet(candidate: BinaryPacket) -> bytes:
    if isinstance(candidate, bytes):
        return candidate
    return bytes(candidate)


def _encode_frame_bytes(command_id: int, payload: bytes) -> bytes:
    """Encapsulate frame creation and COBS encoding."""
    raw_frame = Frame.build(command_id, payload)
    return cobs.encode(raw_frame) + FRAME_DELIMITER


async def serial_sender_not_ready(command_id: int, _: bytes) -> bool:
    logger.warning("Serial disconnected; dropping frame 0x%02X", command_id)
    return False


async def _negotiate_baudrate(
    reader: asyncio.StreamReader,
    writer: asyncio.StreamWriter,
    target_baud: int,
) -> bool:
    """Negotiate a baudrate switch with the MCU."""
    logger.info("Negotiating baudrate switch to %d...", target_baud)
    payload = struct.pack(protocol.UINT32_FORMAT, target_baud)
    encoded = _encode_frame_bytes(Command.CMD_SET_BAUDRATE, payload)

    try:
        writer.write(encoded)
        await writer.drain()
        response_data = await asyncio.wait_for(
            reader.readuntil(FRAME_DELIMITER),
            timeout=SERIAL_BAUDRATE_NEGOTIATION_TIMEOUT,
        )
        decoded = cobs.decode(response_data[:-1])
        resp_frame = Frame.from_bytes(decoded)

        if resp_frame.command_id == Command.CMD_SET_BAUDRATE_RESP:
            logger.info("Baudrate negotiation accepted by MCU.")
            return True
        else:
            logger.warning("Unexpected response: 0x%02X", resp_frame.command_id)
            return False
    except (asyncio.TimeoutError, cobs.DecodeError, ValueError, OSError) as e:
        logger.error("Baudrate negotiation failed: %s", e)
        return False


async def _open_serial_connection_with_retry(
    config: RuntimeConfig,
) -> tuple[asyncio.StreamReader, asyncio.StreamWriter]:
    base_delay = float(max(1, config.reconnect_delay))
    max_delay = base_delay * 8
    current_delay = base_delay

    target_baud = config.serial_baud
    initial_baud = config.serial_safe_baud
    if initial_baud <= 0 or initial_baud == target_baud:
        initial_baud = target_baud
        negotiation_needed = False
    else:
        negotiation_needed = True

    action = f"Serial connection to {config.serial_port}"
    logger.info("Connecting to %s at %d baud...", config.serial_port, initial_baud)

    while True:
        try:
            # Use pyserial-asyncio-fast for zero-overhead I/O
            reader, writer = await serial_asyncio_fast.open_serial_connection(
                url=config.serial_port, baudrate=initial_baud
            )

            if negotiation_needed:
                success = await _negotiate_baudrate(reader, writer, target_baud)
                if success:
                    logger.info("Switching to target baudrate %d...", target_baud)
                    writer.close()
                    await writer.wait_closed()
                    await asyncio.sleep(0.2)

                    reader, writer = await serial_asyncio_fast.open_serial_connection(
                        url=config.serial_port, baudrate=target_baud
                    )
                else:
                    logger.warning(
                        "Negotiation failed; staying at %d baud", initial_baud
                    )

            # [SIL-2] Startup Noise Drain (BOOT LOOP FIX)
            drain_start_time = time.monotonic()
            drain_timeout = 1.0

            try:
                while not reader.at_eof():
                    if (time.monotonic() - drain_start_time) > drain_timeout:
                        logger.warning(
                            "Serial line is flooding (continuous data detected). "
                            "Forcing protocol start after %0.1fs drain. "
                            "Check MCU sketch for rogue Serial.print() calls.",
                            drain_timeout
                        )
                        break

                    try:
                        garbage = await asyncio.wait_for(reader.read(4096), timeout=0.1)
                        if not garbage:
                            break
                        logger.debug("Drained startup noise:\n%s", format_hexdump(garbage, prefix="  "))
                    except asyncio.TimeoutError:
                        logger.debug("Serial line silence detected. Ready.")
                        break
            except (OSError, asyncio.TimeoutError) as e:
                logger.debug("Error draining serial buffer: %s", e)

            return reader, writer

        except (OSError, ExceptionGroup) as exc:
            # pyserial-asyncio-fast raises OSError on connection failure
            if isinstance(exc, ExceptionGroup):
                _, remainder = exc.split((OSError,))
                if remainder:
                    raise remainder

            logger.warning(
                "%s failed (%s); retrying in %.1fs.", action, exc, current_delay
            )
            await asyncio.sleep(current_delay)
            current_delay = min(max_delay, current_delay * 2)
        except asyncio.CancelledError:
            raise


class SerialTransport:
    """Manages the serial connection to the MCU."""

    def __init__(
        self,
        config: RuntimeConfig,
        state: RuntimeState,
        service: BridgeService,
    ) -> None:
        self.config = config
        self.state = state
        self.service = service
        self.reader: asyncio.StreamReader | None = None
        self.writer: asyncio.StreamWriter | None = None

    def _should_emit_parse_error_status(self) -> bool:
        return bool(getattr(self.state, "link_is_synchronized", False))

    async def run(self) -> None:
        reconnect_delay = max(1, self.config.reconnect_delay)

        while True:
            should_retry = True
            try:
                self.reader, self.writer = await _open_serial_connection_with_retry(
                    self.config
                )
                self.state.serial_writer = self.writer
                self.service.register_serial_sender(self.send_frame)
                logger.info("Serial port connected successfully.")

                try:
                    async with asyncio.TaskGroup() as tg:
                        tg.create_task(self._read_loop())
                        await self.service.on_serial_connected()
                except* SerialHandshakeFatal as exc:
                    should_retry = False
                    logger.critical("%s", exc.exceptions[0])
                    raise exc.exceptions[0]
                except* Exception as exc_group:
                    for exc in exc_group.exceptions:
                        logger.critical(
                            "Error running post-connect hooks", exc_info=exc
                        )

            except (OSError, asyncio.IncompleteReadError) as exc:
                logger.error("Serial communication error: %s", exc)
            except ConnectionResetError:
                logger.error("Serial connection reset.")
            except SerialHandshakeFatal:
                raise
            except asyncio.CancelledError:
                logger.info("Serial transport cancelled.")
                raise
            finally:
                await self._disconnect()

            if should_retry:
                logger.warning("Retrying serial in %ds...", reconnect_delay)
                await asyncio.sleep(reconnect_delay)
            else:
                break

    async def _disconnect(self) -> None:
        if self.writer:
            try:
                self.writer.close()
                await self.writer.wait_closed()
            except (OSError, ConnectionError) as exc:
                logger.debug("Error closing serial writer: %s", exc)
        self.writer = None
        self.reader = None
        self.state.serial_writer = None
        self.state.serial_tx_allowed.set()
        try:
            await self.service.on_serial_disconnected()
        except Exception as exc:
            logger.warning("Error in on_serial_disconnected hook: %s", exc)
        self.service.register_serial_sender(serial_sender_not_ready)

    async def _read_loop(self) -> None:
        assert self.reader is not None
        buffer = bytearray()
        discarding = False

        while True:
            try:
                chunk = await self.reader.read(256)
            except (OSError, asyncio.IncompleteReadError) as exc:
                logger.error(f"Serial I/O Error: {exc}")
                break

            if not chunk:
                break

            for byte_value in chunk:
                if byte_value == FRAME_DELIMITER[0]:
                    if discarding:
                        discarding = False
                        buffer.clear()
                        continue

                    if buffer:
                        encoded_packet = bytes(buffer)
                        buffer.clear()
                        await self._process_packet(encoded_packet)
                    continue

                if discarding:
                    continue

                buffer.append(byte_value)
                if len(buffer) > MAX_SERIAL_PACKET_BYTES:
                    snapshot = bytes(buffer[:32])
                    buffer.clear()
                    discarding = True
                    self.state.record_serial_decode_error()
                    logger.warning("Serial packet too large, flushed.")
                    _ = snapshot

    async def send_frame(self, command_id: int, payload: bytes) -> bool:
        writer = self.writer
        if writer is None or writer.is_closing():
            return False

        serial_tx_allowed = getattr(self.state, "serial_tx_allowed", None)
        wait_fn = (
            getattr(serial_tx_allowed, "wait", None)
            if serial_tx_allowed is not None
            else None
        )
        if wait_fn is not None and inspect.iscoroutinefunction(wait_fn):
            await wait_fn()

        writer = self.writer
        if writer is None or writer.is_closing():
            return False
        try:
            encoded = _encode_frame_bytes(command_id, payload)
            writer.write(encoded)
            await writer.drain()

            if logger.isEnabledFor(logging.DEBUG):
                try:
                    cmd_name = Command(command_id).name
                except ValueError:
                    cmd_name = f"0x{command_id:02X}"
                if payload:
                    hexdump = format_hexdump(payload, prefix="       ")
                    logger.debug(
                        "LINUX > %s len=%d\n%s", cmd_name, len(payload), hexdump
                    )
                else:
                    logger.debug("LINUX > %s (no payload)", cmd_name)
            return True
        except OSError as exc:
            logger.error("Send failed 0x%02X: %s", command_id, exc)
            return False

    async def _process_packet(self, encoded_packet: bytes) -> None:
        if not _is_binary_packet(encoded_packet):
            self.state.record_serial_decode_error()
            return

        packet_bytes = _coerce_packet(encoded_packet)
        raw_frame: bytes | None = None

        try:
            raw_frame = cobs.decode(packet_bytes)
            frame = Frame.from_bytes(raw_frame)

            if frame.command_id & protocol.CMD_FLAG_COMPRESSED:
                frame.command_id &= ~protocol.CMD_FLAG_COMPRESSED
                try:
                    frame.payload = rle.decode(frame.payload)
                except ValueError as exc:
                    logger.warning("RLE decompression failed: %s", exc)
                    self.state.record_serial_decode_error()
                    if self._should_emit_parse_error_status():
                        await self.service.send_frame(
                            Status.MALFORMED.value, b"RLE_FAIL"
                        )
                    return

            if logger.isEnabledFor(logging.DEBUG):
                try:
                    cmd_name = Command(frame.command_id).name
                except ValueError:
                    cmd_name = f"0x{frame.command_id:02X}"

                if frame.payload:
                    hexdump = format_hexdump(frame.payload, prefix="       ")
                    logger.debug(
                        "LINUX < %s len=%d\n%s", cmd_name, len(frame.payload), hexdump
                    )
                else:
                    logger.debug("LINUX < %s (no payload)", cmd_name)

            await self.service.handle_mcu_frame(frame.command_id, frame.payload)

        except cobs.DecodeError as exc:
            self.state.record_serial_decode_error()
            header_hex = packet_bytes[:5].hex()
            logger.warning(
                "Frame parse error %s for raw %s (len=%d header=%s)",
                exc,
                packet_bytes.hex(),
                len(packet_bytes),
                header_hex,
            )
            return

        except ValueError as exc:
            self.state.record_serial_decode_error()
            error_data = raw_frame if raw_frame is not None else packet_bytes
            header_hex = error_data[: protocol.CRC_COVERED_HEADER_SIZE].hex()
            logger.warning(
                "Frame parse error %s for raw %s (len=%d header=%s)",
                exc,
                error_data.hex(),
                len(error_data),
                header_hex,
            )

            if not self._should_emit_parse_error_status():
                return

            status = Status.MALFORMED
            if "crc mismatch" in str(exc).lower():
                status = Status.CRC_MISMATCH
                self.state.record_serial_crc_error()

            command_hint = protocol.INVALID_ID_SENTINEL
            if len(error_data) >= protocol.CRC_COVERED_HEADER_SIZE:
                try:
                    _, _, command_hint = struct.unpack(
                        protocol.CRC_COVERED_HEADER_FORMAT,
                        error_data[: protocol.CRC_COVERED_HEADER_SIZE],
                    )
                except struct.error:
                    logger.debug(
                        "Failed to extract command hint from malformed packet",
                        exc_info=True,
                    )

            truncated = error_data[:32]
            payload = struct.pack(protocol.UINT16_FORMAT, command_hint) + truncated
            try:
                await self.service.send_frame(status.value, payload)
            except OSError as exc:
                logger.debug("Failed to send malformed status response: %s", exc)


__all__ = [
    "SerialTransport",
    "serial_sender_not_ready",
    "_open_serial_connection_with_retry",
    "format_hexdump",
    "OPEN_SERIAL_CONNECTION",
]