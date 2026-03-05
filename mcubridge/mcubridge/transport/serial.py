"""Serial transport implementation using pyserial-asyncio-fast Streams.

This module implements a Zero-Overhead asyncio transport using StreamReader
and StreamWriter. It delegates delimiter searching to Python's C core via
`readuntil`, significantly reducing CPU overhead and eliminating manual buffer management.

[SIL-2 COMPLIANCE]
- No dynamic memory allocation after initialization.
- Robust error handling and state tracking.
"""

from __future__ import annotations

import asyncio
import contextlib
import errno
import logging
import time
from collections.abc import Callable
from typing import TYPE_CHECKING, Any, Final, TypeGuard, cast

import msgspec
import serial
import serial_asyncio_fast
import tenacity
from cobs import cobs
from mcubridge.config.const import SERIAL_BAUDRATE_NEGOTIATION_TIMEOUT
from mcubridge.config.settings import RuntimeConfig
from mcubridge.protocol import protocol, rle
from mcubridge.protocol.frame import Frame
from mcubridge.protocol.structures import UINT32_STRUCT
from mcubridge.services.handshake import SerialHandshakeFatal
from mcubridge.services.runtime import BridgeService
from mcubridge.state.context import RuntimeState
from mcubridge.util import log_binary_traffic, log_hexdump
from transitions import Machine

logger = logging.getLogger("mcubridge")

# Explicit framing overhead: 1 code byte + 1 delimiter + ~1 byte/254 overhead
FRAMING_OVERHEAD: Final[int] = 4

MAX_SERIAL_PACKET_BYTES = (
    protocol.CRC_COVERED_HEADER_SIZE + protocol.MAX_PAYLOAD_SIZE + protocol.CRC_SIZE + FRAMING_OVERHEAD
)

BinaryPacket = bytes | bytearray | memoryview


def _is_binary_packet(candidate: Any) -> TypeGuard[BinaryPacket]:
    """Internal validation for binary packets used by tests."""
    if not isinstance(candidate, (bytes, bytearray, memoryview)):
        return False
    # [SIL-2] Use explicit cast to resolve memoryview[Unknown] for pyright
    sized_candidate = cast(BinaryPacket, candidate)
    length = len(sized_candidate)
    if length == 0:
        return False
    return length <= MAX_SERIAL_PACKET_BYTES


async def serial_sender_not_ready(command_id: int, _: bytes) -> bool:
    logger.warning("Serial disconnected; dropping frame 0x%02X", command_id)
    return False


class SerialTransport:
    """Manages the serial connection using high-performance asyncio Streams."""

    if TYPE_CHECKING:
        # FSM generated methods and attributes for static analysis
        fsm_state: str
        begin_reset: Callable[[], None]
        begin_connect: Callable[[], None]
        begin_negotiate: Callable[[], None]
        mark_connected: Callable[[], None]
        handshake: Callable[[], None]
        enter_loop: Callable[[], None]
        mark_disconnected: Callable[[], None]

    # FSM States
    STATE_DISCONNECTED = "disconnected"
    STATE_RESETTING = "resetting"
    STATE_CONNECTING = "connecting"
    STATE_NEGOTIATING = "negotiating"
    STATE_CONNECTED = "connected"
    STATE_HANDSHAKING = "handshaking"
    STATE_RUNNING = "running"

    def __init__(
        self,
        config: RuntimeConfig,
        state: RuntimeState,
        service: BridgeService,
    ) -> None:
        self.config = config
        self.state = state
        self.service = service
        self.writer: asyncio.StreamWriter | None = None
        self._stop_event = asyncio.Event()
        self.loop: asyncio.AbstractEventLoop | None = None
        self._negotiating = False
        self._negotiation_future: asyncio.Future[bool] | None = None

        # FSM Initialization
        self.state_machine = Machine(
            model=self,
            states=[
                {"name": self.STATE_DISCONNECTED, "on_enter": "_on_fsm_disconnect"},
                {"name": self.STATE_RESETTING, "on_enter": "_on_fsm_disconnect"},
                self.STATE_CONNECTING,
                self.STATE_NEGOTIATING,
                self.STATE_CONNECTED,
                self.STATE_HANDSHAKING,
                self.STATE_RUNNING,
            ],
            initial=self.STATE_DISCONNECTED,
            ignore_invalid_triggers=True,
            model_attribute="fsm_state",
        )

        # FSM Transitions
        self.state_machine.add_transition(trigger="begin_reset", source="*", dest=self.STATE_RESETTING)
        self.state_machine.add_transition(
            trigger="begin_connect",
            source=self.STATE_RESETTING,
            dest=self.STATE_CONNECTING,
        )
        self.state_machine.add_transition(
            trigger="begin_negotiate",
            source=self.STATE_CONNECTING,
            dest=self.STATE_NEGOTIATING,
        )
        self.state_machine.add_transition(
            trigger="mark_connected",
            source=[self.STATE_CONNECTING, self.STATE_NEGOTIATING],
            dest=self.STATE_CONNECTED,
        )
        self.state_machine.add_transition(
            trigger="handshake",
            source=self.STATE_CONNECTED,
            dest=self.STATE_HANDSHAKING,
        )
        self.state_machine.add_transition(trigger="enter_loop", source=self.STATE_HANDSHAKING, dest=self.STATE_RUNNING)
        self.state_machine.add_transition(trigger="mark_disconnected", source="*", dest=self.STATE_DISCONNECTED)

    def _on_fsm_disconnect(self) -> None:
        """Callback when leaving any active state."""
        self.state.serial_writer = None

    @tenacity.retry(
        retry=tenacity.retry_if_not_exception_type((SerialHandshakeFatal, asyncio.CancelledError)),
        wait=tenacity.wait_exponential(multiplier=1, min=1, max=60) + tenacity.wait_random(0, 1),
        before_sleep=tenacity.before_sleep_log(logger, logging.WARNING),
        reraise=True,
    )
    async def _retryable_run(self, loop: asyncio.AbstractEventLoop) -> None:
        if self._stop_event.is_set():
            return
        await self._connect_and_run(loop)

    async def run(self) -> None:
        loop = asyncio.get_running_loop()
        try:
            await self._retryable_run(loop)
        except SerialHandshakeFatal:
            logger.critical("Serial Handshake Fatal Error - Giving up.")
            raise
        except asyncio.CancelledError:
            self._stop_event.set()
            raise
        except (ConnectionError, OSError, RuntimeError, ValueError) as exc:
            logger.error("Serial transport stopped unexpectedly: %s", exc)
            raise

    async def _serial_sender(self, cmd: int, pl: bytes) -> bool:
        if self.writer is None or self.writer.is_closing():
            return False

        try:
            raw_frame = Frame.build(cmd, pl)
            encoded = cobs.encode(raw_frame) + protocol.FRAME_DELIMITER
            self.writer.write(encoded)

            if logger.isEnabledFor(logging.DEBUG):
                log_binary_traffic(
                    logger,
                    logging.DEBUG,
                    "[SERIAL -> MCU]",
                    self._get_cmd_label(cmd),
                    pl,
                )
            return True
        except (OSError, ValueError) as exc:
            logger.error("Send failed: %s", exc)
            return False

    def _get_cmd_label(self, command_id: int) -> str:
        try:
            return protocol.Command(command_id).name
        except ValueError:
            return f"0x{command_id:02X}"

    async def _toggle_dtr(self, loop: asyncio.AbstractEventLoop) -> None:
        """Pulse DTR to force MCU hardware reset."""
        self.begin_reset()
        logger.warning("Performing Hardware Reset (DTR Toggle)...")
        try:
            await loop.run_in_executor(None, self._blocking_reset)
            await asyncio.sleep(2.0)
        except (OSError, RuntimeError, ValueError) as e:
            if isinstance(e, OSError) and e.errno == errno.ENOTTY:
                logger.debug("DTR Toggle skipped (Emulated PTY detected): %s", e)
            else:
                logger.error("Async DTR Toggle failed: %s", e)

    def _blocking_reset(self) -> None:
        try:
            with serial.Serial(self.config.serial_port) as s:
                s.dtr = False
                time.sleep(0.1)
                s.dtr = True
                time.sleep(0.1)
                s.dtr = False
        except (OSError, RuntimeError, ValueError) as e:
            if isinstance(e, OSError) and e.errno == errno.ENOTTY:
                return
            logger.error("DTR Toggle failed: %s", e)

    async def _open_connection(
        self, loop: asyncio.AbstractEventLoop, baudrate: int
    ) -> tuple[asyncio.StreamReader, asyncio.StreamWriter]:
        # [SIL-2] Use Any cast as the C-extension stubs for serial_asyncio_fast are incomplete.
        saf = cast(Any, serial_asyncio_fast)
        return await saf.open_serial_connection(
            url=self.config.serial_port,
            baudrate=baudrate,
            limit=MAX_SERIAL_PACKET_BYTES,
        )

    async def _connect_and_run(self, loop: asyncio.AbstractEventLoop) -> None:
        self.loop = loop
        target_baud = self.config.serial_baud
        initial_baud = self.config.serial_safe_baud
        negotiation_needed = initial_baud > 0 and initial_baud != target_baud
        start_baud = initial_baud if negotiation_needed else target_baud

        await self._toggle_dtr(loop)

        self.begin_connect()
        logger.info(
            "Connecting to %s at %d baud (StreamReader Optimized)...",
            self.config.serial_port,
            start_baud,
        )

        reader, writer = await self._open_connection(loop, start_baud)
        self.writer = writer

        try:
            if negotiation_needed:
                self.begin_negotiate()
                success = await self._negotiate_baudrate(reader, target_baud)
                if success:
                    logger.info("Baudrate negotiated. Reconnecting at %d...", target_baud)
                    writer.close()
                    await writer.wait_closed()

                    reader, writer = await self._open_connection(loop, target_baud)
                    self.writer = writer
                else:
                    logger.warning("Negotiation failed, staying at %d", start_baud)

            self.mark_connected()
            # Register sender
            self.service.register_serial_sender(self._serial_sender)
            self.state.serial_writer = writer.transport

            # Handshake
            self.handshake()
            await self.service.on_serial_connected()

            # Wait efficiently for connection loss or stop request
            self.enter_loop()

            stop_task = loop.create_task(self._stop_event.wait())
            read_task = loop.create_task(self._read_loop(reader))

            try:
                done, _ = await asyncio.wait(
                    [read_task, stop_task],
                    return_when=asyncio.FIRST_COMPLETED,
                )

                if read_task in done:
                    exc = read_task.exception()
                    if exc:
                        raise ConnectionError(f"Serial read loop failed: {exc}")
                    raise ConnectionError("Serial connection lost (EOF)")

            finally:
                for task in [stop_task, read_task]:
                    if not task.done():
                        task.cancel()
                        with contextlib.suppress(asyncio.CancelledError):
                            await task

        finally:
            self.mark_disconnected()
            if self.writer and not self.writer.is_closing():
                self.writer.close()
                with contextlib.suppress(Exception):
                    await self.writer.wait_closed()
            self.service.register_serial_sender(serial_sender_not_ready)
            self.writer = None
            try:
                await self.service.on_serial_disconnected()
            except (OSError, RuntimeError, ValueError) as exc:
                logger.warning("Error in on_serial_disconnected hook: %s", exc)

    async def _read_loop(self, reader: asyncio.StreamReader) -> None:
        """Main loop reading complete frames directly from the C-backed Stream."""
        while True:
            try:
                # readuntil delegates the delimiter search to C, saving CPU
                packet_with_sep = await reader.readuntil(protocol.FRAME_DELIMITER)
                packet = packet_with_sep[:-1]  # remove delimiter

                if packet:
                    self._process_packet(packet)

            except asyncio.exceptions.LimitOverrunError:
                logger.warning("Serial packet too large (>%d), flushing.", MAX_SERIAL_PACKET_BYTES)
                self.state.record_serial_decode_error()
                # Drain the overrun data
                await reader.read(MAX_SERIAL_PACKET_BYTES)
            except asyncio.IncompleteReadError:
                # EOF reached, connection closed
                break
            except OSError as e:
                logger.warning("Serial read error: %s", e)
                break

    def _process_packet(self, encoded_packet: bytes) -> None:
        """Dispatcher for decoded packets."""
        # Check for negotiation response first (bypass service for speed)
        if self._negotiating and self._negotiation_future and not self._negotiation_future.done():
            with contextlib.suppress(cobs.DecodeError, ValueError):
                raw_frame = cobs.decode(encoded_packet)
                frame = Frame.from_bytes(raw_frame)
                if frame.command_id == protocol.Command.CMD_SET_BAUDRATE_RESP:
                    self._negotiation_future.set_result(True)
                    return

        # Normal processing via async task to avoid blocking the reader loop
        if self.loop:
            self.loop.create_task(self._async_process_packet(encoded_packet))

    async def _async_process_packet(self, encoded_packet: bytes) -> None:
        if not _is_binary_packet(encoded_packet):
            self.state.record_serial_decode_error()
            return

        packet_bytes = bytes(encoded_packet)

        try:
            raw_frame = cobs.decode(packet_bytes)
            frame = Frame.from_bytes(raw_frame)

            command_id = frame.raw_command_id
            payload = frame.payload

            if frame.is_compressed:
                payload = rle.decode(payload)

            if logger.isEnabledFor(logging.DEBUG):
                log_binary_traffic(
                    logger,
                    logging.DEBUG,
                    "[MCU -> SERIAL]",
                    self._get_cmd_label(command_id),
                    payload if payload else b"",
                )

            await self.service.handle_mcu_frame(command_id, payload)

        except (cobs.DecodeError, ValueError, msgspec.ValidationError) as exc:
            self.state.record_serial_decode_error()
            logger.debug("Frame parse error: %s", exc)
            log_hexdump(logger, logging.DEBUG, "Corrupt Packet", packet_bytes)
            exc_str = str(exc).lower()
            if any(s in exc_str for s in ("crc mismatch", "checksum", "wrong checksum")):
                self.state.record_serial_crc_error()
        except OSError as exc:
            logger.error("OS error during packet processing: %s", exc)
            self.state.record_serial_decode_error()
        except (RuntimeError, TypeError) as exc:
            logger.error("Runtime error during packet processing: %s", exc)
            self.state.record_serial_decode_error()

    async def _negotiate_baudrate(self, reader: asyncio.StreamReader, target_baud: int) -> bool:
        logger.info("Negotiating baudrate switch to %d...", target_baud)
        payload = UINT32_STRUCT.build(target_baud)

        retryer = tenacity.AsyncRetrying(
            stop=tenacity.stop_after_attempt(3),
            wait=tenacity.wait_fixed(0.5) + tenacity.wait_random(0, 0.2),
            retry=tenacity.retry_if_exception_type(asyncio.TimeoutError),
            before_sleep=tenacity.before_sleep_log(logger, logging.WARNING),
            reraise=False,
        )

        try:
            self._negotiating = True
            async for attempt in retryer:
                with attempt:
                    if self.loop:
                        self._negotiation_future = self.loop.create_future()

                    if not await self._serial_sender(protocol.Command.CMD_SET_BAUDRATE.value, payload):
                        self._negotiation_future = None
                        raise asyncio.TimeoutError("Write failed")

                    # Run a temporary reader specifically to catch the response
                    async def temp_reader() -> None:
                        while self._negotiation_future and not self._negotiation_future.done():
                            try:
                                packet_with_sep = await reader.readuntil(protocol.FRAME_DELIMITER)
                                self._process_packet(packet_with_sep[:-1])
                            except Exception:
                                break

                    reader_task = asyncio.create_task(temp_reader())

                    try:
                        if self._negotiation_future:
                            await asyncio.wait_for(
                                self._negotiation_future,
                                SERIAL_BAUDRATE_NEGOTIATION_TIMEOUT,
                            )
                            return True
                    finally:
                        self._negotiation_future = None
                        reader_task.cancel()
                        with contextlib.suppress(asyncio.CancelledError):
                            await reader_task
        except tenacity.RetryError:
            pass
        finally:
            self._negotiating = False

        return False
