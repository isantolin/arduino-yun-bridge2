"""Serial transport helpers for the MCU Bridge daemon (Zero-Overhead Asyncio).

This module provides a native, high-performance serial implementation using
Python's built-in termios and asyncio modules, eliminating external dependencies
like pyserial. It implements "Eager Writes" for minimal latency.
"""

from __future__ import annotations

import asyncio
import fcntl
import logging
import os
import struct
import termios
import time
from typing import Any, Final, Sized, TypeGuard, cast, TYPE_CHECKING

from cobs import cobs
from mcubridge.common import backoff
from mcubridge.rpc import rle
from mcubridge.config.settings import RuntimeConfig
from mcubridge.const import SERIAL_BAUDRATE_NEGOTIATION_TIMEOUT
from mcubridge.rpc.protocol import FRAME_DELIMITER, Command, Status
from mcubridge.rpc import protocol
from mcubridge.rpc.frame import Frame
from mcubridge.services.handshake import SerialHandshakeFatal
from mcubridge.state.context import RuntimeState

if TYPE_CHECKING:
    from mcubridge.services.runtime import BridgeService

logger = logging.getLogger("mcubridge")

# Baudrate constants mapping (Termios)
BAUDRATE_MAP: Final[dict[int, int]] = {
    50: termios.B50, 75: termios.B75, 110: termios.B110, 134: termios.B134,
    150: termios.B150, 200: termios.B200, 300: termios.B300, 600: termios.B600,
    1200: termios.B1200, 1800: termios.B1800, 2400: termios.B2400, 4800: termios.B4800,
    9600: termios.B9600, 19200: termios.B19200, 38400: termios.B38400, 57600: termios.B57600,
    115200: termios.B115200, 230400: termios.B230400, 460800: termios.B460800,
    500000: termios.B500000, 576000: termios.B576000, 921600: termios.B921600,
    1000000: termios.B1000000, 1152000: termios.B1152000, 1500000: termios.B1500000,
    2000000: termios.B2000000, 2500000: termios.B2500000, 3000000: termios.B3000000,
    3500000: termios.B3500000, 4000000: termios.B4000000,
}

class SerialException(OSError):
    """Exception raised on serial port errors."""
    pass

class SerialFileObj:
    """Minimal file-like wrapper for a serial file descriptor."""
    def __init__(self, fd: int):
        self._fd: int | None = fd

    def fileno(self) -> int:
        if self._fd is None:
            raise SerialException("File closed")
        return self._fd

    def close(self) -> None:
        if self._fd is not None:
            try:
                os.close(self._fd)
            except OSError:
                pass
            self._fd = None

    def read(self, size: int) -> bytes:
        if self._fd is None:
            raise SerialException("File closed")
        return os.read(self._fd, size)

    def write(self, data: bytes) -> int:
        if self._fd is None:
            raise SerialException("File closed")
        return os.write(self._fd, data)

def configure_serial_port(fd: int, baudrate: int, exclusive: bool = False) -> None:
    """Configure serial port using termios."""
    if baudrate not in BAUDRATE_MAP:
        raise SerialException(f"Unsupported baudrate: {baudrate}")
    speed = BAUDRATE_MAP[baudrate]

    if exclusive:
        try:
            fcntl.ioctl(fd, termios.TIOCEXCL)
        except (OSError, AttributeError):
            pass

    try:
        attrs = termios.tcgetattr(fd)
    except termios.error as e:
        raise SerialException(f"Failed to get terminal attributes: {e}") from e

    # Raw mode configuration
    attrs[0] = 0  # iflag: no processing
    attrs[1] = 0  # oflag: no processing
    attrs[2] = termios.CS8 | termios.CREAD | termios.CLOCAL # cflag: 8N1
    attrs[3] = 0  # lflag: raw mode
    attrs[6][termios.VMIN] = 0 # cc
    attrs[6][termios.VTIME] = 0 # cc
    attrs[4] = speed  # ispeed
    attrs[5] = speed  # ospeed

    try:
        termios.tcsetattr(fd, termios.TCSANOW, attrs)
        termios.tcflush(fd, termios.TCIOFLUSH)
    except termios.error as e:
        raise SerialException(f"Failed to configure port: {e}") from e

def format_hexdump(data: bytes, prefix: str = "") -> str:
    if not data:
        return f"{prefix}<empty>"
    lines: list[str] = []
    for offset in range(0, len(data), 16):
        chunk = data[offset: offset + 16]
        hex_parts = [" ".join(f"{b:02X}" for b in chunk[i: i + 4]) for i in range(0, 16, 4)]
        hex_str = "  ".join(hex_parts).ljust(47)
        ascii_str = "".join(chr(b) if 32 <= b < 127 else "." for b in chunk)
        lines.append(f"{prefix}{offset:04X}  {hex_str}  |{ascii_str}|")
    return "\n".join(lines)

FRAMING_OVERHEAD: Final[int] = 4
MAX_SERIAL_PACKET_BYTES = (
    protocol.CRC_COVERED_HEADER_SIZE + protocol.MAX_PAYLOAD_SIZE +
    protocol.CRC_SIZE + FRAMING_OVERHEAD
)

BinaryPacket = bytes | bytearray | memoryview

def _is_binary_packet(candidate: object) -> TypeGuard[BinaryPacket]:
    if not isinstance(candidate, (bytes, bytearray, memoryview)):
        return False
    length = len(cast(Sized, candidate))
    return 0 < length <= MAX_SERIAL_PACKET_BYTES

def _coerce_packet(candidate: BinaryPacket) -> bytes:
    return bytes(candidate) if not isinstance(candidate, bytes) else candidate

def _encode_frame_bytes(command_id: int, payload: bytes) -> bytes:
    raw_frame = Frame.build(command_id, payload)
    return cobs.encode(raw_frame) + FRAME_DELIMITER

async def serial_sender_not_ready(command_id: int, _: bytes) -> bool:
    logger.warning("Serial disconnected; dropping frame 0x%02X", command_id)
    return False

# --- Asyncio Protocol Implementation ---

class FlowControlMixin:
    """Implement asyncio flow control logic."""
    def __init__(self, loop: asyncio.AbstractEventLoop | None = None) -> None:
        self._loop = loop or asyncio.get_running_loop()
        self._paused = False
        self._drain_waiter: asyncio.Future[None] | None = None
        self._connection_lost = False

    def pause_writing(self) -> None:
        self._paused = True

    def resume_writing(self) -> None:
        self._paused = False
        if self._drain_waiter and not self._drain_waiter.done():
            self._drain_waiter.set_result(None)
            self._drain_waiter = None

    def connection_lost(self, exc: Exception | None) -> None:
        self._connection_lost = True
        if self._drain_waiter and not self._drain_waiter.done():
            if exc:
                self._drain_waiter.set_exception(exc)
            else:
                self._drain_waiter.set_result(None)
            self._drain_waiter = None

    async def drain_helper(self) -> None:
        if self._connection_lost:
            raise ConnectionResetError("Connection lost")
        if not self._paused:
            return
        if self._drain_waiter is None:
            self._drain_waiter = self._loop.create_future()
        await self._drain_waiter

class SerialReadProtocol(asyncio.Protocol):
    def __init__(self) -> None:
        self.reader = asyncio.StreamReader()

    def connection_made(self, transport: asyncio.BaseTransport) -> None:
        self.reader.set_transport(cast(asyncio.Transport, transport))

    def data_received(self, data: bytes) -> None:
        self.reader.feed_data(data)

    def connection_lost(self, exc: Exception | None) -> None:
        self.reader.feed_eof()

class EagerSerialWriteProtocol(asyncio.Protocol, FlowControlMixin):
    """
    Optimized Write Protocol with Eager Writes.
    Attempts to write directly to the socket/fd first. Only uses the
    asyncio transport buffer if the socket returns EAGAIN.
    """
    def __init__(self, fd: int) -> None:
        FlowControlMixin.__init__(self)
        self._fd = fd
        self.transport: asyncio.Transport | None = None

    def connection_made(self, transport: asyncio.BaseTransport) -> None:
        self.transport = cast(asyncio.Transport, transport)

    def connection_lost(self, exc: Exception | None) -> None:
        FlowControlMixin.connection_lost(self, exc)

    def pause_writing(self) -> None:
        FlowControlMixin.pause_writing(self)

    def resume_writing(self) -> None:
        FlowControlMixin.resume_writing(self)

    def write(self, data: bytes) -> None:
        """Eagerly write data to the file descriptor."""
        if not data:
            return

        # [OPTIMIZATION] Eager Write Strategy
        # Try to write directly to the FD to avoid loop overhead and buffering.
        # If the transport is already paused/buffered, append to it to maintain order.
        if not self._paused and self.transport:
            try:
                n = os.write(self._fd, data)
                if n == len(data):
                    return  # Fully written eagerly! Zero overhead.
                if n > 0:
                    data = data[n:] # Schedule remaining part
            except BlockingIOError:
                pass # Socket full, fall back to transport buffering
            except OSError as e:
                logger.error("Eager write failed: %s", e)
                self.transport.close()
                return

        # Fallback to standard asyncio buffering
        if self.transport:
            self.transport.write(data)

class _WriteProtocolFactory:
    """Factory for EagerSerialWriteProtocol."""
    def __init__(self, fd: int) -> None:
        self._fd = fd
        self.protocol: EagerSerialWriteProtocol | None = None

    def __call__(self) -> EagerSerialWriteProtocol:
        self.protocol = EagerSerialWriteProtocol(self._fd)
        return self.protocol

class _ReadProtocolFactory:
    """Factory for SerialReadProtocol."""
    def __init__(self, proto: SerialReadProtocol) -> None:
        self._proto = proto
    def __call__(self) -> SerialReadProtocol:
        return self._proto

async def _open_serial_connection(
    url: str, baudrate: int, **kwargs: Any
) -> tuple[asyncio.StreamReader, EagerSerialWriteProtocol]:
    """Open a serial connection using native asyncio with Eager Writes."""
    loop = asyncio.get_running_loop()

    # 1. Open FD
    try:
        fd = os.open(url, os.O_RDWR | os.O_NOCTTY | os.O_NONBLOCK)
    except OSError as e:
        raise SerialException(f"Could not open port {url}: {e}") from e

    # 2. Configure FD
    try:
        configure_serial_port(fd, baudrate, exclusive=kwargs.get("exclusive", False))
    except Exception:
        os.close(fd)
        raise

    # 3. Create File Object wrapper for asyncio
    fobj = SerialFileObj(fd)

    # 4. Connect Read Pipe
    read_proto = SerialReadProtocol()
    read_factory = _ReadProtocolFactory(read_proto)
    await loop.connect_read_pipe(read_factory, fobj)

    # Use factory class to avoid nested function/closure
    write_factory = _WriteProtocolFactory(fd)
    _, _ = await loop.connect_write_pipe(write_factory, fobj)

    if write_factory.protocol is None: # pragma: no cover
        raise RuntimeError("Write protocol factory failed to produce protocol")

    return read_proto.reader, write_factory.protocol

async def _negotiate_baudrate(
    reader: asyncio.StreamReader,
    writer: EagerSerialWriteProtocol,
    target_baud: int,
) -> bool:
    logger.info("Negotiating baudrate switch to %d...", target_baud)
    payload = struct.pack(protocol.UINT32_FORMAT, target_baud)
    encoded = _encode_frame_bytes(Command.CMD_SET_BAUDRATE, payload)

    try:
        writer.write(encoded)
        await writer.drain_helper()

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

@backoff(
    retries=-1,
    start_delay=1.0,
    max_delay=8.0,
    jitter=True,
    exceptions=(SerialException, OSError),
)
async def _open_serial_connection_with_retry(
    config: RuntimeConfig,
) -> tuple[asyncio.StreamReader, EagerSerialWriteProtocol]:
    """Open serial connection with declarative backoff/retry."""
    target_baud = config.serial_baud
    initial_baud = config.serial_safe_baud

    negotiation_needed = (initial_baud > 0 and initial_baud != target_baud)
    baud_to_use = initial_baud if negotiation_needed else target_baud

    logger.info("Connecting to %s at %d baud...", config.serial_port, baud_to_use)

    # If this fails, @backoff handles retry/sleep/logging
    reader, writer = await _open_serial_connection(
        url=config.serial_port, baudrate=baud_to_use, exclusive=True
    )

    if negotiation_needed:
        success = await _negotiate_baudrate(reader, writer, target_baud)
        if success:
            logger.info("Switching to target baudrate %d...", target_baud)
            # Close and reopen at new speed
            if writer.transport:
                writer.transport.close()
            # Wait a bit for close to settle?
            await asyncio.sleep(0.2)

            reader, writer = await _open_serial_connection(
                url=config.serial_port, baudrate=target_baud, exclusive=True
            )
        else:
            logger.warning("Negotiation failed; staying at %d baud", baud_to_use)

    # Drain noise
    drain_start = time.monotonic()
    while not reader.at_eof():
        if (time.monotonic() - drain_start) > 1.0:
            break
        try:
            garbage = await asyncio.wait_for(reader.read(4096), timeout=0.1)
            if not garbage:
                break
        except asyncio.TimeoutError:
            break

    return reader, writer

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
        self.writer: EagerSerialWriteProtocol | None = None

    def _should_emit_parse_error_status(self) -> bool:
        return bool(getattr(self.state, "link_is_synchronized", False))

    async def run(self) -> None:
        reconnect_delay = max(1, self.config.reconnect_delay)

        while True:
            should_retry = True
            try:
                # This will retry infinitely internally for connection errors
                self.reader, self.writer = await _open_serial_connection_with_retry(
                    self.config
                )
                self.state.serial_writer = self.writer # type: ignore
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
                        logger.critical("Error running post-connect hooks", exc_info=exc)

            except (SerialException, asyncio.IncompleteReadError) as exc:
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
        if self.writer and self.writer.transport:
            try:
                self.writer.transport.close()
            except (OSError, ConnectionError):
                pass
        self.writer = None
        self.reader = None
        self.state.serial_writer = None
        self.state.serial_tx_allowed.set()
        try:
            await self.service.on_serial_disconnected()
        except Exception as exc:
            logger.warning("Error in on_serial_disconnected: %s", exc)
        self.service.register_serial_sender(serial_sender_not_ready)

    async def _read_loop(self) -> None:
        assert self.reader is not None
        buffer = bytearray()
        discarding = False

        while True:
            try:
                chunk = await self.reader.read(256)
            except (OSError, asyncio.IncompleteReadError):
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
                    discarding = True
                    self.state.record_serial_decode_error()
                    logger.warning("Serial packet too large, flushed.")

    async def send_frame(self, command_id: int, payload: bytes) -> bool:
        writer = self.writer
        if writer is None or writer.transport is None or writer.transport.is_closing():
            return False

        serial_tx_allowed = getattr(self.state, "serial_tx_allowed", None)
        if serial_tx_allowed:
             await serial_tx_allowed.wait()

        writer = self.writer
        if writer is None or writer.transport is None or writer.transport.is_closing():
            return False

        try:
            encoded = _encode_frame_bytes(command_id, payload)
            writer.write(encoded)
            await writer.drain_helper()

            if logger.isEnabledFor(logging.DEBUG):
                try:
                    cmd_name = Command(command_id).name
                except ValueError:
                    cmd_name = f"0x{command_id:02X}"
                if payload:
                    hexdump = format_hexdump(payload, prefix="       ")
                    logger.debug("LINUX > %s len=%d\n%s", cmd_name, len(payload), hexdump)
                else:
                    logger.debug("LINUX > %s (no payload)", cmd_name)
            return True
        except (OSError, SerialException) as exc:
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
                except ValueError:
                    self.state.record_serial_decode_error()
                    if self._should_emit_parse_error_status():
                        await self.service.send_frame(Status.MALFORMED.value, b"RLE_FAIL")
                    return

            if logger.isEnabledFor(logging.DEBUG):
                try:
                    cmd_name = Command(frame.command_id).name
                except ValueError:
                    cmd_name = f"0x{frame.command_id:02X}"
                if frame.payload:
                    hexdump = format_hexdump(frame.payload, prefix="       ")
                    logger.debug("LINUX < %s len=%d\n%s", cmd_name, len(frame.payload), hexdump)
                else:
                    logger.debug("LINUX < %s (no payload)", cmd_name)

            await self.service.handle_mcu_frame(frame.command_id, frame.payload)

        except cobs.DecodeError:
            self.state.record_serial_decode_error()
            return
        except ValueError as exc:
            self.state.record_serial_decode_error()
            if self._should_emit_parse_error_status():
                status = Status.CRC_MISMATCH if "crc mismatch" in str(exc).lower() else Status.MALFORMED
                if status == Status.CRC_MISMATCH:
                    self.state.record_serial_crc_error()

                # Attempt to extract hint
                command_hint = protocol.INVALID_ID_SENTINEL
                data_to_parse = raw_frame if 'raw_frame' in locals() and raw_frame else packet_bytes
                if len(data_to_parse) >= protocol.CRC_COVERED_HEADER_SIZE:
                    try:
                        _, _, command_hint = struct.unpack(
                            protocol.CRC_COVERED_HEADER_FORMAT,
                            data_to_parse[: protocol.CRC_COVERED_HEADER_SIZE]
                        )
                    except struct.error:
                        pass

                truncated = data_to_parse[:32]
                payload = struct.pack(protocol.UINT16_FORMAT, command_hint) + truncated
                await self.service.send_frame(status.value, payload)

__all__ = [
    "SerialTransport",
    "serial_sender_not_ready",
    "_open_serial_connection_with_retry",
    "format_hexdump",
]
