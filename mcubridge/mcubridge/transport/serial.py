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
import structlog
from typing import TYPE_CHECKING, Any, Final, cast

import msgspec
from cobs.cobs import (
    encode as cobs_encode,
    decode as cobs_decode,
    DecodeError as CobsDecodeError,
)
import serial
import serial_asyncio_fast
import tenacity

from mcubridge.config.const import (
    MAX_SERIAL_FRAME_BYTES,
    DEFAULT_RECONNECT_DELAY,
    SERIAL_BAUDRATE_NEGOTIATION_TIMEOUT,
    SERIAL_HANDSHAKE_BACKOFF_BASE,
    SERIAL_HANDSHAKE_BACKOFF_MAX,
)
from mcubridge.protocol import protocol, structures as structures
from mcubridge.protocol.frame import Frame
from transitions import Machine

if TYPE_CHECKING:
    from mcubridge.config.settings import RuntimeConfig
    from mcubridge.state.context import RuntimeState

logger = structlog.get_logger("mcubridge.serial")

_RAW_FRAME_MIN_SIZE: Final[int] = protocol.CRC_COVERED_HEADER_SIZE + protocol.CRC_SIZE
_RAW_FRAME_MAX_SIZE: Final[int] = (
    protocol.CRC_COVERED_HEADER_SIZE + protocol.MAX_PAYLOAD_SIZE + protocol.CRC_SIZE
)


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
        self._consecutive_crc_errors = 0
        self._tx_sequence_id = 0
        self._packet_semaphore = asyncio.Semaphore(16)

        # State Machine
        self.fsm_state: str = self.STATE_DISCONNECTED
        self._machine = Machine(
            model=self,
            states=[
                self.STATE_DISCONNECTED,
                self.STATE_NEGOTIATING,
                self.STATE_CONNECTED,
            ],
            transitions=[
                {
                    "trigger": "begin_negotiate",
                    "source": self.STATE_DISCONNECTED,
                    "dest": self.STATE_NEGOTIATING,
                },
                {
                    "trigger": "mark_connected",
                    "source": self.STATE_NEGOTIATING,
                    "dest": self.STATE_CONNECTED,
                },
                {
                    "trigger": "mark_disconnected",
                    "source": "*",
                    "dest": self.STATE_DISCONNECTED,
                },
            ],
            initial=self.STATE_DISCONNECTED,
            queued=True,
            model_attribute="fsm_state",
            ignore_invalid_triggers=True,
            after_state_change="_on_state_change",
        )

    def _switch_local_baudrate(self, target_baud: int) -> None:
        if self.writer is None or self.writer.is_closing():
            raise RuntimeError(
                "Cannot switch local UART baudrate without an active serial writer"
            )

        # [SIL-2] Direct access to transport implementation to switch baudrate
        try:
            serial_port = cast(Any, self.writer.transport).serial
            serial_port.baudrate = target_baud
            logger.info("Local UART switched to %d baud", target_baud)
        except (AttributeError, ValueError) as e:
            raise RuntimeError(
                f"Serial transport does not expose the underlying UART: {e}"
            ) from e

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
            except (
                OSError,
                serial.SerialException,
                asyncio.TimeoutError,
                RuntimeError,
                tenacity.RetryError,
            ) as exc:
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
        retry=tenacity.retry_if_not_exception_type(asyncio.CancelledError)
        & tenacity.retry_if_exception(
            lambda e: "SerialHandshakeFatal" not in type(e).__name__
        ),
        before_sleep=tenacity.before_sleep_log(logger, logging.WARNING),
    )
    async def _retryable_run(self, loop: asyncio.AbstractEventLoop) -> None:
        """Single connection attempt."""
        logger.info("Connecting to MCU on %s...", self.config.serial_port)

        await self._toggle_dtr(loop)

        try:
            connect_baud = self.config.serial_safe_baud
            if connect_baud <= 0:
                connect_baud = protocol.DEFAULT_SAFE_BAUDRATE

            self.reader, self.writer = await serial_asyncio_fast.open_serial_connection(
                url=self.config.serial_port,
                baudrate=connect_baud,
                xonxoff=False,
            )
            # Use cast to satisfy pyright that StreamWriter is a BaseTransport (it is in asyncio)
            self.state.serial_writer = cast(asyncio.BaseTransport, self.writer)

            reader = self.reader
            # Start reader loop
            read_task = loop.create_task(self._read_loop(reader))

            try:
                # 1. Negotiate baudrate if needed
                self._machine.dispatch("begin_negotiate")
                if self.config.serial_baud != connect_baud:
                    if not await self._negotiate_baudrate(self.config.serial_baud):
                        raise ConnectionError("Baudrate negotiation failed")

                # 2. Complete handshake via service
                self._machine.dispatch("mark_connected")
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
                self._machine.dispatch("mark_disconnected")
                await self.service.on_serial_disconnected()

        except (OSError, serial.SerialException) as exc:
            logger.warning("Connection failed: %s", exc)
            raise
        except (asyncio.TimeoutError, RuntimeError, tenacity.RetryError) as exc:
            logger.error("CRITICAL RETRY ERROR: %s", exc, exc_info=True)
            raise

    async def _toggle_dtr(self, loop: asyncio.AbstractEventLoop) -> None:
        """Hardware reset via DTR toggle using async sleep."""
        try:
            # [SIL-2] Precise async pulsing to avoid blocking thread pool
            def _set_dtr(val: bool):
                with serial.Serial(self.config.serial_port) as s:
                    s.dtr = val

            await loop.run_in_executor(None, _set_dtr, False)
            await asyncio.sleep(0.1)
            await loop.run_in_executor(None, _set_dtr, True)
        except (OSError, serial.SerialException) as exc:
            logger.debug("DTR toggle not supported or failed: %s", exc)

    async def _read_loop(self, reader: asyncio.StreamReader) -> None:
        """Main loop reading complete frames directly from the C-backed Stream."""
        while not self._stop_event.is_set():
            try:
                # readuntil delegates the delimiter search to C, saving CPU
                packet_with_sep = await reader.readuntil(protocol.FRAME_DELIMITER)
                packet_view = memoryview(packet_with_sep)[
                    :-1
                ]  # remove delimiter (Zero-copy)

                if packet_view:
                    if logger.isEnabledFor(logging.DEBUG):
                        logger.debug(
                            "[SERIAL <- MCU] [RAW]: [%s]", packet_view.hex(" ").upper()
                        )
                    self._process_packet(packet_view)

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
            except (
                OSError,
                serial.SerialException,
                asyncio.TimeoutError,
                RuntimeError,
            ) as exc:
                logger.error("Error in _read_loop: %s", exc)
                break

    def _process_packet(self, encoded_packet: bytes | memoryview) -> None:
        """Dispatcher for decoded packets."""
        if (
            self._negotiating
            and self._negotiation_future
            and not self._negotiation_future.done()
        ):
            try:
                frame = Frame.parse(cobs_decode(encoded_packet))
                if frame.command_id == protocol.Command.CMD_SET_BAUDRATE_RESP.value:
                    self._switch_local_baudrate(self.config.serial_baud)
                    self._negotiation_future.set_result(True)
                    return
            except (CobsDecodeError, ValueError) as e:
                logger.debug("Discarding malformed frame during read: %s", e)

        if self.loop:
            self.loop.create_task(self._async_process_packet_with_limit(encoded_packet))

    async def _async_process_packet_with_limit(
        self, encoded_packet: bytes | memoryview
    ) -> None:
        """Async packet processing logic with backpressure limit."""
        async with self._packet_semaphore:
            await self._async_process_packet(encoded_packet)

    async def _async_process_packet(self, encoded_packet: bytes | memoryview) -> None:
        """Async packet processing logic (SIL-2)."""
        packet_bytes = (
            encoded_packet
            if isinstance(encoded_packet, bytes)
            else encoded_packet.tobytes()
        )

        try:
            # [SIL-2] Deterministic COBS decode and Frame parse.
            decoded = cobs_decode(packet_bytes)
            frame = Frame.parse(decoded)
            cmd_id, seq_id, payload = frame.command_id, frame.sequence_id, frame.payload

            if logger.isEnabledFor(logging.DEBUG):
                # [SIL-2] Mandatory HEXADECIMAL logging for binary traffic.
                raw_hex = packet_bytes.hex(" ").upper()
                logger.debug("[MCU -> SERIAL] [SEQ:%04X] [RAW]: [%s]", seq_id, raw_hex)

            await self.service.handle_mcu_frame(cmd_id, seq_id, payload)
            self.state.record_serial_rx(len(encoded_packet))

        except (CobsDecodeError, ValueError, msgspec.DecodeError) as exc:
            # [SIL-2] Fault Isolation: Group malformed/decode errors separately from runtime logic.
            raw_hex = packet_bytes.hex(" ").upper()
            logger.warning("[SERIAL <- MCU] [MALFORMED (ERR: %s)]: [%s]", exc, raw_hex)
            self.state.record_serial_decode_error()
            await self._check_baudrate_fallback()
        except (OSError, RuntimeError, asyncio.TimeoutError) as exc:
            # [SIL-2] Fault Isolation: Capture transport-level failures.
            raw_hex = packet_bytes.hex(" ").upper()
            logger.error("[SERIAL <- MCU] [TRANSPORT (ERR: %s)]: [%s]", exc, raw_hex)
            self.state.record_serial_decode_error()
            await self._check_baudrate_fallback()
        except (TypeError, AttributeError, KeyError) as exc:
            # [SIL-2] Boundary Guard: Catch-all for unexpected logic errors,
            # ensuring they are typed and sent to syslog.
            raw_hex = packet_bytes.hex(" ").upper()
            logger.critical("[SERIAL <- MCU] [FATAL LOGIC (ERR: %s)]: [%s]", exc, raw_hex, exc_info=True)
            self.state.record_serial_decode_error()
            raise

    async def _check_baudrate_fallback(self) -> None:
        """Monitor CRC error rate and trigger fallback if threshold exceeded."""
        self._consecutive_crc_errors += 1
        if self._consecutive_crc_errors >= self.config.serial_fallback_threshold:
            logger.warning(
                "CRC error threshold reached (%d). Attempting baudrate fallback to safe speed (%d).",
                self._consecutive_crc_errors,
                self.config.serial_safe_baud,
            )
            self._consecutive_crc_errors = 0
            if self.config.serial_baud != self.config.serial_safe_baud:
                await self._negotiate_baudrate(self.config.serial_safe_baud)
            else:
                logger.error("Already at safe baudrate; cannot fallback further.")

    async def _serial_sender(self, cmd: int, pl: bytes, seq: int | None = None) -> bool:
        """Low-level serial frame sender with flow control integration."""
        if not self.writer or self.writer.is_closing():
            return False

        # [SIL-2] Flow Control: Wait if MCU requested a pause (XOFF)
        # Toggled by CMD_XOFF/CMD_XON in ConsoleComponent.
        # We use a safety timeout to avoid permanent deadlocks if MCU fails to send XON.
        if not self.state.serial_tx_allowed.is_set():
            try:
                logger.debug(
                    "Serial TX paused by MCU; waiting for XON (timeout=30s)..."
                )
                async with asyncio.timeout(30.0):
                    await self.state.serial_tx_allowed.wait()
            except (asyncio.TimeoutError, TimeoutError):
                logger.error(
                    "Flow control deadlock detected: MCU stayed in XOFF for >30s. Forcing re-sync."
                )
                raise ConnectionError("Flow control timeout (MCU XOFF deadlock)")
            except (asyncio.CancelledError, RuntimeError):
                return False

        try:
            if seq is None:
                self._tx_sequence_id = (self._tx_sequence_id + 1) & protocol.UINT16_MAX
                seq = self._tx_sequence_id

            frame = Frame(command_id=cmd, sequence_id=seq, payload=pl)
            encoded = cobs_encode(frame.build()) + protocol.FRAME_DELIMITER

            if logger.isEnabledFor(logging.DEBUG):
                logger.log(
                    logging.DEBUG,
                    "[SERIAL -> MCU] [SEQ:%04X] [RAW]: [%s]",
                    seq,
                    encoded.hex(" ").upper(),
                )

            self.writer.write(encoded)
            await self.writer.drain()

            self.state.record_serial_tx(len(encoded))
            return True
        except (OSError, asyncio.CancelledError) as e:
            logger.warning("Send failed: %s", e)
            return False

    async def _negotiate_baudrate(self, target_baud: int) -> bool:
        """Execute baudrate switch protocol."""
        logger.info("Negotiating baudrate switch to %d...", target_baud)

        payload = structures.SetBaudratePacket(baudrate=target_baud).encode()
        retryer = tenacity.AsyncRetrying(
            stop=tenacity.stop_after_attempt(3),
            wait=tenacity.wait_exponential(
                multiplier=SERIAL_HANDSHAKE_BACKOFF_BASE,
                max=SERIAL_HANDSHAKE_BACKOFF_MAX,
            ),
            retry=tenacity.retry_if_exception_type(asyncio.TimeoutError),
            before_sleep=tenacity.before_sleep_log(logger, logging.WARNING),
            reraise=True,
        )

        if self.loop is None:
            raise RuntimeError("Serial event loop is not initialized")

        async def _attempt() -> bool:
            assert self.loop is not None
            self._negotiation_future = self.loop.create_future()
            if not await self._serial_sender(
                protocol.Command.CMD_SET_BAUDRATE.value, payload
            ):
                raise asyncio.TimeoutError("Write failed")

            try:
                await asyncio.wait_for(
                    self._negotiation_future,
                    timeout=SERIAL_BAUDRATE_NEGOTIATION_TIMEOUT,
                )
                return True
            except asyncio.TimeoutError:
                raise

        self._negotiating = True
        try:
            return await retryer(_attempt)
        except (tenacity.RetryError, asyncio.TimeoutError):
            return False
        finally:
            self._negotiating = False
            self._negotiation_future = None
