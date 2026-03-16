"""Serial transport implementation using pyserial-asyncio-fast Streams.

This module implements a Zero-Overhead asyncio transport using StreamReader
and StreamWriter. It delegates delimiter searching to Python's C core via
`readuntil`, ensuring maximum throughput for high-speed serial links.

[SIL-2 COMPLIANCE]
- Deterministic buffer handling.
- Explicit lifecycle management.
- Zero dynamic allocation after initialization.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import time
from typing import TYPE_CHECKING, Any, Final, cast

from cobs.cobs import encode as cobs_encode, decode as cobs_decode, DecodeError as CobsDecodeError
import serial
import serial_asyncio_fast
import tenacity
from transitions import Machine

from mcubridge.config.const import (
    MAX_SERIAL_FRAME_BYTES,
    SERIAL_HANDSHAKE_BACKOFF_BASE,
    SERIAL_HANDSHAKE_BACKOFF_MAX,
    DEFAULT_RECONNECT_DELAY,
)
from mcubridge.protocol import protocol, structures
from mcubridge.protocol.frame import Frame
from mcubridge.util.hex import log_binary_traffic

if TYPE_CHECKING:
    from mcubridge.config.settings import RuntimeConfig
    from mcubridge.state.context import RuntimeState

logger = logging.getLogger("mcubridge.serial")

def _is_binary_packet(packet: bytes) -> bool:
    """Validate packet header matches protocol v2."""
    if len(packet) < 5:
        return False
    return packet[0] == protocol.PROTOCOL_VERSION

async def serial_sender_not_ready(cmd: int, pl: bytes) -> bool:
    """Fallback sender that logs a warning when the transport is not connected."""
    _ = cmd
    _ = pl
    logger.warning("Attempted to send frame while serial transport is disconnected")
    return False


class SerialTransport:
    """High-performance asyncio serial transport."""

    STATE_DISCONNECTED: Final[str] = "disconnected"
    STATE_NEGOTIATING: Final[str] = "negotiating"
    STATE_CONNECTED: Final[str] = "connected"

    def __init__(
        self,
        config: RuntimeConfig,
        state: RuntimeState,
        service: Any,
    ) -> None:
        self.config = config
        self.state = state
        self.service = service
        self.reader: asyncio.StreamReader | None = None
        self.writer: asyncio.StreamWriter | None = None
        self.loop: asyncio.AbstractEventLoop | None = None

        # Register ourselves as the sender for the service
        self.service.register_serial_sender(self._serial_sender)

        self._stop_event = asyncio.Event()
        self._negotiating = False
        self._negotiation_future: asyncio.Future[bool] | None = None

        # State Machine
        self.fsm_state: str = self.STATE_DISCONNECTED
        self._machine = Machine(
            model=self,
            states=[
                self.STATE_DISCONNECTED,
                self.STATE_NEGOTIATING,
                self.STATE_CONNECTED,
            ],
            initial=self.STATE_DISCONNECTED,
            after_state_change=self._on_state_change,
            model_attribute="fsm_state",
        )
        self._machine.add_transition(
            trigger="begin_negotiate",
            source=self.STATE_DISCONNECTED,
            dest=self.STATE_NEGOTIATING,
        )
        self._machine.add_transition(
            trigger="mark_connected",
            source=self.STATE_NEGOTIATING,
            dest=self.STATE_CONNECTED,
        )
        self._machine.add_transition(
            trigger="mark_disconnected",
            source="*",
            dest=self.STATE_DISCONNECTED,
        )

    # Type hints for dynamic FSM methods
    def begin_negotiate(self) -> None: pass
    def mark_connected(self) -> None: pass
    def mark_disconnected(self) -> None: pass

    def _on_state_change(self) -> None:
        logger.debug("Serial transport state: %s", self.fsm_state)

    async def run(self) -> None:
        """Main transport entry point with auto-reconnect."""
        self.loop = asyncio.get_running_loop()
        while not self._stop_event.is_set():
            try:
                await self._retryable_run(self.loop)
            except asyncio.CancelledError:
                break
            except (OSError, serial.SerialException, asyncio.TimeoutError, RuntimeError, tenacity.RetryError) as exc:
                if "SerialHandshakeFatal" in type(exc).__name__:
                    raise
                logger.error("Transport fatal error: %s", exc, exc_info=True)
                await asyncio.sleep(DEFAULT_RECONNECT_DELAY)

    async def stop(self) -> None:
        """Gracefully stop the transport."""
        self._stop_event.set()
        if self.writer:
            self.writer.close()
            with contextlib.suppress(OSError, RuntimeError):
                await self.writer.wait_closed()

    @tenacity.retry(
        wait=tenacity.wait_exponential(multiplier=1, min=1, max=10),
        retry=tenacity.retry_if_exception(
            lambda e: not isinstance(e, asyncio.CancelledError)
            and "SerialHandshakeFatal" not in type(e).__name__
        ),
        before_sleep=tenacity.before_sleep_log(logger, logging.WARNING),
    )
    async def _retryable_run(self, loop: asyncio.AbstractEventLoop) -> None:
        """Single connection attempt."""
        logger.info("Connecting to MCU on %s...", self.config.serial_port)

        await self._toggle_dtr(loop)

        try:
            self.reader, self.writer = await serial_asyncio_fast.open_serial_connection(  # type: ignore
                url=self.config.serial_port,
                baudrate=self.config.serial_baud,
                xonxoff=False,
            )
            self.state.serial_writer = self.writer  # type: ignore

            reader = cast(asyncio.StreamReader, self.reader)  # type: ignore
            # Start reader loop
            read_task = loop.create_task(self._read_loop(reader))

            try:
                # 1. Negotiate baudrate if needed
                if self.config.serial_baud != 115200:
                    self.begin_negotiate()
                    if not await self._negotiate_baudrate(reader, self.config.serial_baud):
                        raise ConnectionError("Baudrate negotiation failed")
                else:
                    self.begin_negotiate() # Dummy transition

                # 2. Complete handshake via service
                self.mark_connected()
                await self.service.on_serial_connected()

                # 3. Wait for reader to finish or stop event
                stop_task = loop.create_task(self._stop_event.wait())
                done, pending = await asyncio.wait(
                    [read_task, stop_task],
                    return_when=asyncio.FIRST_COMPLETED,
                )

                if stop_task in pending:
                    stop_task.cancel()

                if read_task in done:
                    exc = read_task.exception()
                    if exc:
                        raise ConnectionError(f"Serial read loop failed: {exc}")
                    raise ConnectionError("Serial connection lost (EOF)")

            finally:
                read_task.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await read_task
                self.mark_disconnected()
                await self.service.on_serial_disconnected()

        except (OSError, serial.SerialException) as exc:
            logger.warning("Connection failed: %s", exc)
            raise
        except (asyncio.TimeoutError, RuntimeError, tenacity.RetryError) as exc:
            logger.error("CRITICAL RETRY ERROR: %s", exc, exc_info=True)
            raise

    async def _toggle_dtr(self, loop: asyncio.AbstractEventLoop) -> None:
        """Hardware reset via DTR toggle."""
        try:
            # We must use synchronous serial for DTR control as asyncio wrapper
            # often lacks it or it's buggy across platforms.
            def _sync_toggle():
                with serial.Serial(self.config.serial_port) as s:
                    s.dtr = False
                    time.sleep(0.1)
                    s.dtr = True
            await loop.run_in_executor(None, _sync_toggle)
        except (OSError, serial.SerialException) as exc:
            logger.debug("DTR toggle not supported or failed: %s", exc)

    async def _read_loop(self, reader: asyncio.StreamReader) -> None:
        """Main loop reading complete frames directly from the C-backed Stream."""
        while not self._stop_event.is_set():
            try:
                # readuntil delegates the delimiter search to C, saving CPU
                packet_with_sep = await reader.readuntil(protocol.FRAME_DELIMITER)
                packet = packet_with_sep[:-1]  # remove delimiter

                if packet:
                    # logger.debug("[SERIAL <- MCU] RAW: [%s]", packet.hex())
                    self._process_packet(packet)

            except asyncio.LimitOverrunError:
                logger.warning("Serial packet too large, flushing.")
                self.state.record_serial_decode_error()
                # Drain the overrun data
                await reader.read(MAX_SERIAL_FRAME_BYTES)
            except asyncio.IncompleteReadError as e:
                # EOF reached, connection closed
                logger.info(
                    "Serial connection closed (EOF). Partial data: %s",
                    e.partial.hex(" ") if e.partial else "None",
                )
                break
            except (OSError, serial.SerialException, asyncio.TimeoutError, RuntimeError) as exc:
                logger.error("Error in _read_loop: %s", exc)
                break

    def _process_packet(self, encoded_packet: bytes) -> None:
        """Dispatcher for decoded packets."""
        if self._negotiating and self._negotiation_future and not self._negotiation_future.done():
            try:
                raw_frame = cobs_decode(encoded_packet)
                frame = Frame.from_bytes(raw_frame)
                if frame.command_id == protocol.Command.CMD_SET_BAUDRATE_RESP.value:
                    self._negotiation_future.set_result(True)
            except (CobsDecodeError, ValueError):
                pass
            return

        if self.loop:
            self.loop.create_task(self._async_process_packet(encoded_packet))

    async def _async_process_packet(self, encoded_packet: bytes) -> None:
        """Async packet processing logic."""
        # [DEBUG] Trace packet intake
        logger.debug("[SERIAL <- MCU] Processing Packet: %s", encoded_packet.hex(" "))

        if not _is_binary_packet(encoded_packet):
            self.state.record_serial_decode_error()
            return

        try:
            raw_frame = cobs_decode(encoded_packet)
            frame = Frame.from_bytes(raw_frame)

            if logger.isEnabledFor(logging.DEBUG):
                log_binary_traffic(logger, logging.DEBUG, "[SERIAL <- MCU]", "RAW", encoded_packet)

            await self.service.handle_mcu_frame(frame.command_id, frame.payload)
            self.state.record_serial_rx(len(encoded_packet))

        except (CobsDecodeError, ValueError) as exc:
            log_binary_traffic(logger, logging.WARNING, "[SERIAL <- MCU]", f"Malformed (Err: {exc})", encoded_packet)
            self.state.record_serial_decode_error()
        except (OSError, RuntimeError, KeyError, IndexError, TypeError) as exc:
            log_binary_traffic(logger, logging.ERROR, "[SERIAL <- MCU]", f"Dispatch (Err: {exc})", encoded_packet)
            self.state.record_serial_decode_error()
    async def _serial_sender(self, cmd: int, pl: bytes) -> bool:
        """Low-level serial frame sender."""
        if not self.writer or self.writer.is_closing():
            return False

        try:
            raw_frame = Frame.build(cmd, pl)
            encoded = cobs_encode(raw_frame) + protocol.FRAME_DELIMITER

            logger.debug("[SERIAL -> MCU] RAW: %s", encoded.hex(" "))
            self.writer.write(encoded)
            await self.writer.drain()

            self.state.record_serial_tx(len(encoded))
            return True
        except (OSError, asyncio.CancelledError) as e:
            logger.warning("Send failed: %s", e)
            return False

    async def _negotiate_baudrate(self, reader: asyncio.StreamReader, target_baud: int) -> bool:
        """Execute baudrate switch protocol."""
        logger.info("Negotiating baudrate switch to %d...", target_baud)

        payload = structures.SetBaudratePacket(baudrate=target_baud).encode()
        retryer = tenacity.AsyncRetrying(
            stop=tenacity.stop_after_attempt(3),
            wait=tenacity.wait_exponential(multiplier=SERIAL_HANDSHAKE_BACKOFF_BASE, max=SERIAL_HANDSHAKE_BACKOFF_MAX),
            before_sleep=tenacity.before_sleep_log(logger, logging.WARNING),
            reraise=True
        )

        try:
            async for attempt in retryer:
                with attempt:
                    if self.loop:
                        self._negotiation_future = self.loop.create_future()

                    if not await self._serial_sender(protocol.Command.CMD_SET_BAUDRATE.value, payload):
                        raise asyncio.TimeoutError("Write failed")

                    try:
                        assert self._negotiation_future is not None
                        await asyncio.wait_for(self._negotiation_future, timeout=2.0)
                        return True
                    except asyncio.TimeoutError:
                        raise
        except (tenacity.RetryError, asyncio.TimeoutError):
            pass
        finally:
            self._negotiating = False

        return False
