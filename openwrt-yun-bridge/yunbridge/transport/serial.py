"""Serial transport helpers for the Yun Bridge daemon."""

from __future__ import annotations

import asyncio
import logging
import struct
try:
    import termios
    import tty
except ImportError:
    termios = None  # type: ignore
    tty = None  # type: ignore
from typing import Any, Sized, TypeGuard, cast

import serial
import serial_asyncio
from cobs import cobs

from yunbridge.config.settings import RuntimeConfig
from yunbridge.const import SERIAL_TERMINATOR
from yunbridge.rpc import protocol
from yunbridge.rpc.frame import Frame
from yunbridge.rpc.protocol import Command, Status
from yunbridge.services.runtime import (
    BridgeService,
    SerialHandshakeFatal,
)
from yunbridge.state.context import RuntimeState

logger = logging.getLogger("yunbridge")

OPEN_SERIAL_CONNECTION = serial_asyncio.open_serial_connection

MAX_SERIAL_PACKET_BYTES = (
    protocol.CRC_COVERED_HEADER_SIZE + protocol.MAX_PAYLOAD_SIZE + protocol.CRC_SIZE + 4
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


async def serial_sender_not_ready(command_id: int, _: bytes) -> bool:
    logger.warning(
        "Serial disconnected; dropping frame 0x%02X",
        command_id,
    )
    return False


async def _negotiate_baudrate(
    reader: asyncio.StreamReader,
    writer: asyncio.StreamWriter,
    target_baud: int,
) -> bool:
    """Negotiate a baudrate switch with the MCU."""
    logger.info("Negotiating baudrate switch to %d...", target_baud)

    # Construct CMD_SET_BAUDRATE frame
    payload = struct.pack(">I", target_baud)
    frame = Frame(
        command_id=Command.CMD_SET_BAUDRATE,
        payload=payload,
    )
    encoded = cobs.encode(frame.pack()) + b"\x00"  # pyright: ignore[reportUnknownMemberType, reportUnknownArgumentType, reportAttributeAccessIssue]

    try:
        writer.write(encoded)
        await writer.drain()

        # Wait for ACK (CMD_SET_BAUDRATE_RESP)
        # We expect a quick response.
        response_data = await asyncio.wait_for(reader.readuntil(b"\x00"), timeout=2.0)

        # Decode and verify
        decoded = cobs.decode(response_data[:-1])
        resp_frame = Frame.unpack(decoded)  # pyright: ignore[reportUnknownVariableType, reportUnknownMemberType, reportAttributeAccessIssue]

        if resp_frame.command_id == Command.CMD_SET_BAUDRATE_RESP:  # pyright: ignore[reportUnknownMemberType]
            logger.info("Baudrate negotiation accepted by MCU.")
            return True
        else:
            logger.warning(
                "Unexpected response during baudrate negotiation: 0x%02X",
                resp_frame.command_id,  # pyright: ignore[reportUnknownMemberType, reportUnknownArgumentType]
            )
            return False

    except (asyncio.TimeoutError, cobs.DecodeError, Exception) as e:
        logger.error("Baudrate negotiation failed: %s", e)
        return False


async def _open_serial_connection_with_retry(
    config: RuntimeConfig,
) -> tuple[asyncio.StreamReader, asyncio.StreamWriter]:
    """Establish serial connection with native exponential backoff."""
    base_delay = float(max(1, config.reconnect_delay))
    max_delay = base_delay * 8
    current_delay = base_delay

    target_baud = config.serial_baud
    initial_baud = config.serial_safe_baud

    # If safe baud is not configured or same as target, just use target
    if initial_baud <= 0 or initial_baud == target_baud:
        initial_baud = target_baud
        negotiation_needed = False
    else:
        negotiation_needed = True

    action = f"Serial connection to {config.serial_port}"
    logger.info(
        "Connecting to serial port %s at %d baud (target: %d)...",
        config.serial_port,
        initial_baud,
        target_baud,
    )

    while True:
        try:
            reader, writer = await OPEN_SERIAL_CONNECTION(
                url=config.serial_port,
                baudrate=initial_baud,
                exclusive=True,
            )

            # Force raw mode to disable ECHO and other processing that might confuse the protocol
            if termios and tty:
                try:
                    transport = cast(Any, writer.transport)
                    if hasattr(transport, "serial"):
                        ser = transport.serial
                        if hasattr(ser, "fd") and ser.fd is not None:
                            # tty.setraw disables ECHO, ICANON, ISIG, and sets CS8
                            tty.setraw(ser.fd)

                            # Explicitly ensure ECHO is off (setraw should do it, but be sure)
                            attrs = termios.tcgetattr(ser.fd)
                            attrs[3] = attrs[3] & ~termios.ECHO
                            termios.tcsetattr(ser.fd, termios.TCSANOW, attrs)

                            logger.debug("Forced raw mode (no echo) on %s", config.serial_port)
                except Exception as e:
                    logger.warning("Failed to force raw mode on serial port: %s", e)

            if negotiation_needed:
                success = await _negotiate_baudrate(reader, writer, target_baud)
                if success:
                    logger.info("Switching to target baudrate %d...", target_baud)
                    writer.close()
                    await writer.wait_closed()
                    # Small delay to let MCU switch
                    await asyncio.sleep(0.1)

                    # Reopen at target baud
                    reader, writer = await OPEN_SERIAL_CONNECTION(
                        url=config.serial_port,
                        baudrate=target_baud,
                        exclusive=True,
                    )
                    # Re-apply raw mode (omitted for brevity, but ideally should be a helper)
                    # For now, we assume the previous raw mode setting persists or we re-apply it.
                    # Actually, closing and reopening might reset termios.
                    # So we should re-apply raw mode.
                    if termios and tty:
                        try:
                            transport = cast(Any, writer.transport)
                            if hasattr(transport, "serial"):
                                ser = transport.serial
                                if hasattr(ser, "fd") and ser.fd is not None:
                                    tty.setraw(ser.fd)
                                    attrs = termios.tcgetattr(ser.fd)
                                    attrs[3] = attrs[3] & ~termios.ECHO
                                    termios.tcsetattr(ser.fd, termios.TCSANOW, attrs)
                        except Exception:
                            pass
                else:
                    logger.warning("Negotiation failed; falling back to safe baudrate %d", initial_baud)
                    # We continue with initial_baud (safe)

            return reader, writer
        except (serial.SerialException, OSError, ExceptionGroup) as exc:
            if isinstance(exc, ExceptionGroup):
                _, remainder = exc.split((serial.SerialException, OSError))
                if remainder:
                    raise remainder

            logger.warning(
                "%s failed (%s); retrying in %.1fs.",
                action,
                exc,
                current_delay,
            )
            try:
                await asyncio.sleep(current_delay)
            except asyncio.CancelledError:
                logger.debug("%s retry loop cancelled", action)
                raise

            # Exponential backoff
            current_delay = min(max_delay, current_delay * 2)

        except asyncio.CancelledError:
            logger.debug("%s cancelled", action)
            raise
        except Exception:
            logger.critical("Unexpected error during serial connection", exc_info=True)
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

    async def run(self) -> None:
        """Main loop for the serial transport."""
        reconnect_delay = max(1, self.config.reconnect_delay)

        while True:
            should_retry = True
            try:
                await self._connect()
                assert self.reader is not None
                assert self.writer is not None

                # Register sender
                self.service.register_serial_sender(self.send_frame)
                logger.info("Serial port connected successfully.")

                read_task = asyncio.create_task(self._read_loop())

                try:
                    await self.service.on_serial_connected()
                except SerialHandshakeFatal as exc:
                    should_retry = False
                    logger.critical("%s", exc)
                    read_task.cancel()
                    try:
                        await read_task
                    except asyncio.CancelledError:
                        pass
                    raise
                except Exception:
                    logger.exception("Error running post-connect hooks for serial link")

                await read_task

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
                    "Unhandled exception in SerialTransport.run",
                    exc_info=True,
                )
            finally:
                await self._disconnect()
                if should_retry:
                    logger.warning(
                        "Serial port disconnected. Retrying in %d seconds...",
                        reconnect_delay,
                    )
                    await asyncio.sleep(reconnect_delay)

    async def _connect(self) -> None:
        self.reader, self.writer = await _open_serial_connection_with_retry(self.config)
        self.state.serial_writer = self.writer

    async def _disconnect(self) -> None:
        if self.writer and not self.writer.is_closing():
            try:
                self.writer.close()
                await self.writer.wait_closed()
            except Exception:
                logger.exception("Error closing serial writer during cleanup.")
        self.writer = None
        self.reader = None
        self.state.serial_writer = None
        try:
            await self.service.on_serial_disconnected()
        except Exception:
            logger.exception("Error resetting service state after serial disconnect")
        self.service.register_serial_sender(serial_sender_not_ready)

    async def _read_loop(self) -> None:
        assert self.reader is not None
        buffer = bytearray()
        while True:
            byte = await self.reader.read(1)
            if not byte:
                logger.warning("Serial stream ended; reconnecting.")
                break

            if byte == SERIAL_TERMINATOR:
                if not buffer:
                    continue
                encoded_packet = bytes(buffer)
                buffer.clear()
                await self._process_packet(encoded_packet)
            else:
                buffer.append(byte[0])
                if len(buffer) > MAX_SERIAL_PACKET_BYTES:
                    snapshot = bytes(buffer[:32])
                    buffer.clear()
                    self.state.record_serial_decode_error()
                    logger.warning(
                        "Serial packet exceeded %d bytes; requesting retransmit.",
                        MAX_SERIAL_PACKET_BYTES,
                    )
                    payload = struct.pack(">H", 0xFFFF) + snapshot
                    try:
                        await self.service.send_frame(
                            Status.MALFORMED.value,
                            payload,
                        )
                    except Exception:
                        logger.exception(
                            "Failed to notify MCU about oversized serial packet"
                        )

    async def send_frame(self, command_id: int, payload: bytes) -> bool:
        if self.writer is None or self.writer.is_closing():
            logger.error(
                "Serial writer closed; cannot send frame 0x%02X",
                command_id,
            )
            return False

        try:
            raw_frame = Frame(command_id, payload).to_bytes()
            encoded_frame = cobs.encode(raw_frame) + SERIAL_TERMINATOR
            self.writer.write(encoded_frame)
            await self.writer.drain()

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
            logger.error("Refusing to send frame 0x%02X: %s", command_id, exc)
            return False
        except ConnectionResetError:
            logger.error(
                "Serial connection reset while sending frame 0x%02X",
                command_id,
            )
            await self._disconnect()
            return False
        except Exception:
            logger.exception("Unexpected error sending frame 0x%02X", command_id)
            return False

    async def _process_packet(self, encoded_packet: object) -> None:
        if not _is_binary_packet(encoded_packet):
            logger.warning(
                "Dropping non-binary serial packet type %s",
                type(encoded_packet).__name__,
            )
            self.state.record_serial_decode_error()
            payload = struct.pack(">H", 0xFFFF)
            try:
                await self.service.send_frame(Status.MALFORMED.value, payload)
            except Exception:
                logger.exception("Failed to notify MCU about non-binary serial payload")
            return

        packet_bytes = _coerce_packet(encoded_packet)
        try:
            raw_frame = cobs.decode(packet_bytes)
        except cobs.DecodeError as exc:
            packet_hex = packet_bytes.hex()
            logger.warning(
                "COBS decode error %s for packet %s (len=%d)",
                exc,
                packet_hex,
                len(packet_bytes),
            )
            if logger.isEnabledFor(logging.DEBUG):
                appended = packet_bytes + SERIAL_TERMINATOR
                human_hex = " ".join(f"{byte:02x}" for byte in appended)
                logger.debug(
                    "Decode error raw bytes (len=%d): %s",
                    len(appended),
                    human_hex,
                )
            self.state.record_serial_decode_error()
            truncated = packet_bytes[:32]
            payload = struct.pack(">H", 0xFFFF) + truncated
            try:
                await self.service.send_frame(Status.MALFORMED.value, payload)
            except Exception:
                logger.exception("Failed to request MCU retransmission after decode error")
            return

        try:
            frame = Frame.from_bytes(raw_frame)
        except ValueError as exc:
            header_hex = raw_frame[: protocol.CRC_COVERED_HEADER_SIZE].hex()
            logger.warning(
                ("Frame parse error %s for raw %s (len=%d header=%s)"),
                exc,
                raw_frame.hex(),
                len(raw_frame),
                header_hex,
            )
            status = Status.MALFORMED
            if "crc mismatch" in str(exc).lower():
                status = Status.CRC_MISMATCH
                self.state.record_serial_crc_error()
            command_hint = 0xFFFF
            if len(raw_frame) >= protocol.CRC_COVERED_HEADER_SIZE:
                _, _, command_hint = struct.unpack(
                    protocol.CRC_COVERED_HEADER_FORMAT,
                    raw_frame[: protocol.CRC_COVERED_HEADER_SIZE],
                )
            truncated = raw_frame[:32]
            payload = struct.pack(">H", command_hint) + truncated
            try:
                await self.service.send_frame(status.value, payload)
            except Exception:
                logger.exception("Failed to notify MCU about frame parse error")
            return
        except Exception:
            logger.exception("Unhandled error processing MCU frame")
            return

        try:
            await self.service.handle_mcu_frame(frame.command_id, frame.payload)
        except Exception:
            logger.exception("Unhandled error processing MCU frame")


async def serial_reader_task(
    config: RuntimeConfig,
    state: RuntimeState,
    service: BridgeService,
) -> None:
    """Legacy wrapper for SerialTransport."""
    transport = SerialTransport(config, state, service)
    await transport.run()


__all__ = [
    "MAX_SERIAL_PACKET_BYTES",
    "serial_reader_task",
    "serial_sender_not_ready",
]
