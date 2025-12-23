"""Serial transport helpers for the Yun Bridge daemon."""

from __future__ import annotations

import asyncio
import logging
import struct
import os
try:
    import termios
    import tty
except ImportError:
    termios = None  # type: ignore
    tty = None  # type: ignore
from typing import Any, Sized, TypeGuard, cast

import serial
from cobs import cobs

from yunbridge.config.settings import RuntimeConfig
from yunbridge.const import FRAME_DELIMITER
from yunbridge.rpc import protocol
from yunbridge.rpc.frame import Frame
from yunbridge.rpc.protocol import Command, Status
from yunbridge.services.runtime import (
    BridgeService,
    SerialHandshakeFatal,
)
from yunbridge.state.context import RuntimeState

logger = logging.getLogger("yunbridge")

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


def _encode_frame_bytes(command_id: int, payload: bytes) -> bytes:
    """Encapsulate frame creation and COBS encoding."""
    raw_frame = Frame(command_id, payload).to_bytes()
    return cobs.encode(raw_frame) + FRAME_DELIMITER


def _ensure_raw_mode(serial_obj: Any, port_name: str) -> None:
    """Force raw mode on the serial file descriptor if possible."""
    if not (termios and tty):
        return

    try:
        if hasattr(serial_obj, "fd") and serial_obj.fd is not None:
            tty.setraw(serial_obj.fd)
            attrs = termios.tcgetattr(serial_obj.fd)
            attrs[3] = attrs[3] & ~termios.ECHO
            termios.tcsetattr(serial_obj.fd, termios.TCSANOW, attrs)
            logger.debug("Forced raw mode (no echo) on %s", port_name)
    except Exception as e:
        logger.warning("Failed to force raw mode on serial port: %s", e)


async def serial_sender_not_ready(command_id: int, _: bytes) -> bool:
    logger.warning("Serial disconnected; dropping frame 0x%02X", command_id)
    return False


class SerialProtocol(asyncio.Protocol):
    """Native asyncio Protocol for Serial communication."""
    def __init__(self) -> None:
        self.transport: asyncio.Transport | None = None
        self.reader: asyncio.StreamReader = asyncio.StreamReader()
        
    def connection_made(self, transport: asyncio.BaseTransport) -> None:
        self.transport = cast(asyncio.Transport, transport)
        self.reader.set_transport(transport)
        
    def data_received(self, data: bytes) -> None:
        self.reader.feed_data(data)
        
    def connection_lost(self, exc: Exception | None) -> None:
        self.reader.feed_eof()


async def _open_serial_connection(
    url: str, baudrate: int
) -> tuple[asyncio.StreamReader, asyncio.StreamWriter]:
    """
    Open a serial connection using native asyncio.
    Replaces pyserial-asyncio to ensure Python 3.13+ compatibility.
    """
    loop = asyncio.get_running_loop()
    
    # Create the pyserial instance in a non-blocking way
    ser = serial.serial_for_url(url, baudrate=baudrate, do_not_open=True)
    ser.exclusive = True
    
    def _open_hardware() -> None:
        try:
            ser.open()
            # Set non-blocking on the file descriptor
            os.set_blocking(ser.fd, False)
        except Exception:
            if ser.is_open:
                ser.close()
            raise

    await loop.run_in_executor(None, _open_hardware)
    
    transport, protocol_instance = await loop.connect_read_pipe(
        SerialProtocol, 
        ser
    )
    
    writer = asyncio.StreamWriter(transport, protocol_instance, None, loop)
    return protocol_instance.reader, writer


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
        response_data = await asyncio.wait_for(reader.readuntil(FRAME_DELIMITER), timeout=2.0)
        decoded = cobs.decode(response_data[:-1])
        resp_frame = Frame.from_bytes(decoded)

        if resp_frame.command_id == Command.CMD_SET_BAUDRATE_RESP:
            logger.info("Baudrate negotiation accepted by MCU.")
            return True
        else:
            logger.warning("Unexpected response: 0x%02X", resp_frame.command_id)
            return False
    except (asyncio.TimeoutError, cobs.DecodeError, Exception) as e:
        logger.error("Baudrate negotiation failed: %s", e)
        return False


async def _open_serial_with_retry(
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
            reader, writer = await _open_serial_connection(
                url=config.serial_port,
                baudrate=initial_baud,
            )

            # Access underlying serial object to force raw mode
            try:
                transport = writer.transport
                if hasattr(transport, "_serial"): # Standard asyncio pipe transport
                    _ensure_raw_mode(transport._serial, config.serial_port) # type: ignore
                elif hasattr(transport, "serial"): # Some other transports
                    _ensure_raw_mode(transport.serial, config.serial_port) # type: ignore
            except Exception as e:
                logger.debug("Could not set raw mode (might be ignored): %s", e)

            if negotiation_needed:
                success = await _negotiate_baudrate(reader, writer, target_baud)
                if success:
                    logger.info("Switching to target baudrate %d...", target_baud)
                    writer.close()
                    await writer.wait_closed()
                    await asyncio.sleep(0.2)
                    
                    reader, writer = await _open_serial_connection(
                        url=config.serial_port,
                        baudrate=target_baud,
                    )
                else:
                    logger.warning("Negotiation failed; staying at %d baud", initial_baud)

            return reader, writer

        except (serial.SerialException, OSError, ExceptionGroup) as exc:
            logger.warning("%s failed (%s); retrying in %.1fs.", action, exc, current_delay)
            await asyncio.sleep(current_delay)
            current_delay = min(max_delay, current_delay * 2)
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
        reconnect_delay = max(1, self.config.reconnect_delay)

        while True:
            should_retry = True
            try:
                self.reader, self.writer = await _open_serial_with_retry(self.config)
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
                except* Exception:
                    logger.exception("Error running post-connect hooks")

            except (serial.SerialException, asyncio.IncompleteReadError) as exc:
                logger.error("Serial communication error: %s", exc)
            except ConnectionResetError:
                logger.error("Serial connection reset.")
            except SerialHandshakeFatal:
                raise
            except asyncio.CancelledError:
                logger.info("Serial transport cancelled.")
                raise
            except Exception:
                logger.critical("Unhandled exception in SerialTransport", exc_info=True)
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
            except Exception:
                pass
        self.writer = None
        self.reader = None
        self.state.serial_writer = None
        try:
            await self.service.on_serial_disconnected()
        except Exception:
            pass
        self.service.register_serial_sender(serial_sender_not_ready)

    async def _read_loop(self) -> None:
        assert self.reader is not None
        buffer = bytearray()
        while True:
            try:
                # Read byte-by-byte for simplicity with COBS
                byte = await self.reader.read(1)
            except (OSError, asyncio.IncompleteReadError):
                break
                
            if not byte:
                break

            if byte == FRAME_DELIMITER:
                if buffer:
                    encoded_packet = bytes(buffer)
                    buffer.clear()
                    await self._process_packet(encoded_packet)
                    # Yield to event loop to allow processing
                    await asyncio.sleep(0)
            else:
                buffer.append(byte[0])
                if len(buffer) > MAX_SERIAL_PACKET_BYTES:
                    # Flush and warn
                    buffer.clear()
                    self.state.record_serial_decode_error()
                    logger.warning("Serial packet too large, flushed.")
                    try:
                        payload = struct.pack(protocol.UINT16_FORMAT, protocol.INVALID_ID_SENTINEL)
                        await self.service.send_frame(Status.MALFORMED.value, payload)
                    except Exception:
                        pass

    async def send_frame(self, command_id: int, payload: bytes) -> bool:
        if self.writer is None or self.writer.is_closing():
            return False
        try:
            encoded = _encode_frame_bytes(command_id, payload)
            self.writer.write(encoded)
            await self.writer.drain()
            
            # Debug logging
            if logger.isEnabledFor(logging.DEBUG):
                try:
                    cmd_name = Command(command_id).name
                except ValueError:
                    cmd_name = f"0x{command_id:02X}"
                logger.debug("LINUX > %s payload=%s", cmd_name, payload.hex())
            return True
        except Exception as exc:
            logger.error("Send failed 0x%02X: %s", command_id, exc)
            return False

    async def _process_packet(self, encoded_packet: bytes) -> None:
        # Same processing logic as before, just kept concise here
        if not _is_binary_packet(encoded_packet):
            return

        try:
            packet_bytes = _coerce_packet(encoded_packet)
            raw_frame = cobs.decode(packet_bytes)
            frame = Frame.from_bytes(raw_frame)
            await self.service.handle_mcu_frame(frame.command_id, frame.payload)
        except (cobs.DecodeError, ValueError) as e:
            logger.warning("Frame decode error: %s", e)
            self.state.record_serial_decode_error()
        except Exception:
            logger.exception("Error processing frame")

__all__ = ["SerialTransport", "serial_sender_not_ready"]
