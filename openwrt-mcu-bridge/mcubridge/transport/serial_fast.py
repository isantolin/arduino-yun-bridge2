"""Serial transport implementation using pyserial-asyncio-fast with direct Protocol access.

This module implements a Zero-Overhead asyncio Protocol for serial communication,
bypassing the StreamReader/StreamWriter abstraction to minimize latency and
avoid double-buffering. It uses eager writes to the underlying file descriptor.
"""

from __future__ import annotations

import asyncio
import functools
import logging
import msgspec
from typing import Any, Final, Sized, TypeGuard, cast

import tenacity
from cobs import cobs
from mcubridge.rpc import rle

# [SIL-2] Deterministic Import: pyserial-asyncio-fast is MANDATORY.
# Do not catch ImportError. Fail immediately if dependency is missing.
import serial_asyncio_fast  # type: ignore

from mcubridge.common import log_hexdump
from mcubridge.config.settings import RuntimeConfig
from mcubridge.const import SERIAL_BAUDRATE_NEGOTIATION_TIMEOUT
from mcubridge.rpc import protocol
from mcubridge.rpc.frame import Frame
from mcubridge.services.runtime import BridgeService
from mcubridge.state.context import RuntimeState

# Import directly from handshake to avoid circular dependency
from mcubridge.services.handshake import SerialHandshakeFatal

logger = logging.getLogger("mcubridge")


# Explicit framing overhead: 1 code byte + 1 delimiter + ~1 byte/254 overhead
FRAMING_OVERHEAD: Final[int] = 4

MAX_SERIAL_PACKET_BYTES = (
    protocol.CRC_COVERED_HEADER_SIZE + protocol.MAX_PAYLOAD_SIZE + protocol.CRC_SIZE + FRAMING_OVERHEAD
)

BinaryPacket = bytes | bytearray | memoryview


def _is_binary_packet(candidate: object) -> TypeGuard[BinaryPacket]:
    if not isinstance(candidate, (bytes, bytearray, memoryview)):
        return False
    length = len(cast(Sized, candidate))
    if length == 0:
        return False
    return length <= MAX_SERIAL_PACKET_BYTES


def _encode_frame_bytes(command_id: int, payload: bytes) -> bytes:
    """Encapsulate frame creation and COBS encoding."""
    raw_frame = Frame.build(command_id, payload)
    return cobs.encode(raw_frame) + protocol.FRAME_DELIMITER


async def serial_sender_not_ready(command_id: int, _: bytes) -> bool:
    logger.warning("Serial disconnected; dropping frame 0x%02X", command_id)
    return False


def _log_baud_retry(retry_state: tenacity.RetryCallState) -> None:
    if retry_state.attempt_number > 1:
        logger.warning("Baudrate negotiation timed out (attempt %d); retrying...", retry_state.attempt_number)


class BridgeSerialProtocol(asyncio.Protocol):
    """Zero-Overhead AsyncIO Protocol for MCU Bridge."""

    def __init__(
        self,
        service: BridgeService,
        state: RuntimeState,
        loop: asyncio.AbstractEventLoop,
    ) -> None:
        self.service = service
        self.state = state
        self.loop = loop
        self.transport: asyncio.Transport | None = None
        self._buffer = bytearray()
        self.connected_future: asyncio.Future[None] = loop.create_future()
        self.negotiation_future: asyncio.Future[bool] | None = None

        # Discard state
        self._discarding = False

    def connection_made(self, transport: asyncio.BaseTransport) -> None:
        self.transport = cast(asyncio.Transport, transport)
        logger.info("Serial transport established (Protocol).")
        if not self.connected_future.done():
            self.connected_future.set_result(None)

    def connection_lost(self, exc: Exception | None) -> None:
        logger.warning("Serial connection lost: %s", exc)
        self.transport = None
        if not self.connected_future.done():
            self.connected_future.set_exception(exc or ConnectionError("Closed"))

    def data_received(self, data: bytes) -> None:
        """Handle incoming data with zero-copy splicing where possible."""
        # Fast path: if no buffer and data contains full packets
        if not self._buffer and not self._discarding and protocol.FRAME_DELIMITER in data:
            self._process_chunk_fast(data)
            return

        # Slow path: accumulation
        for byte_val in data:
            if byte_val == 0x00:  # protocol.FRAME_DELIMITER
                if self._discarding:
                    self._discarding = False
                    self._buffer.clear()
                    continue

                if self._buffer:
                    self._process_packet(bytes(self._buffer))
                    self._buffer.clear()
                continue

            if self._discarding:
                continue

            self._buffer.append(byte_val)
            if len(self._buffer) > MAX_SERIAL_PACKET_BYTES:
                logger.warning("Serial packet too large (>%d), flushing.", MAX_SERIAL_PACKET_BYTES)
                self.state.record_serial_decode_error()
                self._buffer.clear()
                self._discarding = True

    def _process_chunk_fast(self, data: bytes) -> None:
        start = 0
        while True:
            try:
                end = data.index(protocol.FRAME_DELIMITER, start)
                packet = data[start:end]
                if packet:
                    self._process_packet(packet)
                start = end + 1
            except ValueError:
                # No more delimiters, buffer remainder
                if start < len(data):
                    self._buffer.extend(data[start:])
                break

    def _process_packet(self, encoded_packet: bytes) -> None:
        # Check for negotiation response first (bypass service)
        if self.negotiation_future and not self.negotiation_future.done():
            try:
                raw_frame = cobs.decode(encoded_packet)
                frame = Frame.from_bytes(raw_frame)
                if frame.command_id == protocol.Command.CMD_SET_BAUDRATE_RESP:
                    self.negotiation_future.set_result(True)
                    return
            except (cobs.DecodeError, ValueError):
                pass  # Ignore malformed during negotiation

        # Normal processing
        self.loop.create_task(self._async_process_packet(encoded_packet))

    async def _async_process_packet(self, encoded_packet: bytes) -> None:
        if not _is_binary_packet(encoded_packet):
            self.state.record_serial_decode_error()
            return

        try:
            raw_frame = cobs.decode(bytes(encoded_packet))
            frame = Frame.from_bytes(raw_frame)

            if frame.command_id & protocol.CMD_FLAG_COMPRESSED:
                frame.command_id &= ~protocol.CMD_FLAG_COMPRESSED
                frame.payload = rle.decode(frame.payload)

            if logger.isEnabledFor(logging.DEBUG):
                self._log_frame(frame, "MCU >")

            await self.service.handle_mcu_frame(frame.command_id, frame.payload)

        except (cobs.DecodeError, ValueError, msgspec.ValidationError) as exc:
            self.state.record_serial_decode_error()
            logger.debug("Frame parse error: %s", exc)
            log_hexdump(logger, logging.DEBUG, "Corrupt Packet", bytes(encoded_packet))
            if "crc mismatch" in str(exc).lower():
                self.state.record_serial_crc_error()
        except OSError as exc:
            logger.error("OS error during packet processing: %s", exc)
            self.state.record_serial_decode_error()

    def _log_frame(self, frame: Frame, direction: str) -> None:
        try:
            cmd_name = protocol.Command(frame.command_id).name
        except ValueError:
            cmd_name = f"0x{frame.command_id:02X}"

        if frame.payload:
            log_hexdump(logger, logging.DEBUG, f"{direction} {cmd_name}", frame.payload)
        else:
            logger.debug("%s %s (no payload)", direction, cmd_name)

    def write_frame(self, command_id: int, payload: bytes) -> bool:
        if self.transport is None or self.transport.is_closing():
            return False

        try:
            encoded = _encode_frame_bytes(command_id, payload)
            self.transport.write(encoded)

            if logger.isEnabledFor(logging.DEBUG):
                try:
                    cmd_name = protocol.Command(command_id).name
                except ValueError:
                    cmd_name = f"0x{command_id:02X}"
                if payload:
                    log_hexdump(logger, logging.DEBUG, f"LINUX > {cmd_name}", payload)
                else:
                    logger.debug("LINUX > %s (no payload)", cmd_name)
            return True
        except (OSError, ValueError) as exc:
            logger.error("Send failed: %s", exc)
            return False


class SerialTransport:
    """Manages the serial connection using the high-performance Protocol."""

    def __init__(
        self,
        config: RuntimeConfig,
        state: RuntimeState,
        service: BridgeService,
    ) -> None:
        self.config = config
        self.state = state
        self.service = service
        self.protocol: BridgeSerialProtocol | None = None
        self._stop_event = asyncio.Event()

    def _before_sleep_log(self, retry_state: tenacity.RetryCallState) -> None:
        reconnect_delay = max(1, self.config.reconnect_delay)
        if retry_state.attempt_number > 1:
            logger.warning(
                "Retrying serial connection in %ds... (Attempt %d)",
                reconnect_delay,
                retry_state.attempt_number,
            )

    async def run(self) -> None:
        reconnect_delay = max(1, self.config.reconnect_delay)
        loop = asyncio.get_running_loop()

        retryer = tenacity.AsyncRetrying(
            retry=tenacity.retry_if_not_exception_type(
                (SerialHandshakeFatal, asyncio.CancelledError)
            ),
            wait=tenacity.wait_fixed(reconnect_delay),
            before_sleep=self._before_sleep_log,
            reraise=True,
        )

        try:
            async for attempt in retryer:
                with attempt:
                    if self._stop_event.is_set():
                        # Clean exit if stopped
                        return
                    await self._connect_and_run(loop)
        except SerialHandshakeFatal:
            logger.critical("Serial Handshake Fatal Error - Giving up.")
            raise
        except asyncio.CancelledError:
            self._stop_event.set()
            raise
        except Exception as exc:
            # Fallback for unexpected errors that bubbled up
            logger.error("Serial transport stopped unexpectedly: %s", exc)
            raise

    async def _serial_sender(self, cmd: int, pl: bytes) -> bool:
        if self.protocol:
            return self.protocol.write_frame(cmd, pl)
        return False

    async def _connect_and_run(self, loop: asyncio.AbstractEventLoop) -> None:
        target_baud = self.config.serial_baud
        initial_baud = self.config.serial_safe_baud
        negotiation_needed = initial_baud > 0 and initial_baud != target_baud
        start_baud = initial_baud if negotiation_needed else target_baud

        # 1. Connect
        logger.info("Connecting to %s at %d baud (Fast Protocol)...", self.config.serial_port, start_baud)

        protocol_factory = functools.partial(BridgeSerialProtocol, self.service, self.state, loop)
        transport, proto = await serial_asyncio_fast.create_serial_connection(
            loop, protocol_factory, self.config.serial_port, baudrate=start_baud
        )
        self.protocol = cast(BridgeSerialProtocol, proto)
        await self.protocol.connected_future

        try:
            # 2. Negotiate if needed
            if negotiation_needed:
                success = await self._negotiate_baudrate(self.protocol, target_baud)
                if success:
                    logger.info("Baudrate negotiated. Reconnecting at %d...", target_baud)
                    transport.close()
                    # Wait for close?
                    await asyncio.sleep(0.2)

                    transport, proto = await serial_asyncio_fast.create_serial_connection(
                        loop, protocol_factory, self.config.serial_port, baudrate=target_baud
                    )
                    self.protocol = cast(BridgeSerialProtocol, proto)
                    await self.protocol.connected_future
                else:
                    logger.warning("Negotiation failed, staying at %d", start_baud)

            # 3. Register Sender
            self.service.register_serial_sender(self._serial_sender)
            self.state.serial_writer = transport

            # 4. Handshake / Main Loop
            # Since Protocol handles reading in background, we just wait here
            # or perform the handshake logic.
            # Service.on_serial_connected handles handshake (HELLO/SYNC)
            await self.service.on_serial_connected()

            # Keep alive until connection lost
            while not transport.is_closing():
                await asyncio.sleep(1)

            raise ConnectionError("Serial connection lost")

        finally:
            if transport and not transport.is_closing():
                transport.close()
            self.service.register_serial_sender(serial_sender_not_ready)
            self.protocol = None
            try:
                await self.service.on_serial_disconnected()
            except (OSError, ValueError, RuntimeError) as exc:
                logger.warning("Error in on_serial_disconnected hook: %s", exc)

    async def _negotiate_baudrate(self, proto: BridgeSerialProtocol, target_baud: int) -> bool:
        logger.info("Negotiating baudrate switch to %d...", target_baud)
        payload = cast(Any, protocol.UINT32_STRUCT).build(target_baud)

        # [SIL-2] Retry logic for baudrate negotiation
        retryer = tenacity.AsyncRetrying(
            stop=tenacity.stop_after_attempt(3),
            wait=tenacity.wait_fixed(0.5),
            retry=tenacity.retry_if_exception_type(asyncio.TimeoutError),
            before_sleep=_log_baud_retry,
            reraise=False,
        )

        try:
            async for attempt in retryer:
                with attempt:
                    proto.negotiation_future = proto.loop.create_future()
                    if not proto.write_frame(protocol.Command.CMD_SET_BAUDRATE.value, payload):
                        proto.negotiation_future = None
                        raise asyncio.TimeoutError("Write failed")

                    try:
                        await asyncio.wait_for(proto.negotiation_future, SERIAL_BAUDRATE_NEGOTIATION_TIMEOUT)
                        return True
                    finally:
                        proto.negotiation_future = None
        except tenacity.RetryError:
            pass

        return False
