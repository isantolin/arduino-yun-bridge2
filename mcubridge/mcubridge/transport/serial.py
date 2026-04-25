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
from typing import TYPE_CHECKING, Any, cast

import msgspec
from cobs import cobs
import serial
import serial_asyncio_fast
import time
from serial import SerialException
import tenacity

from mcubridge.services.handshake import SerialHandshakeFatal
from mcubridge.config.const import (
    MAX_SERIAL_FRAME_BYTES,
    DEFAULT_RECONNECT_DELAY,
    SERIAL_BAUDRATE_NEGOTIATION_TIMEOUT,
    SERIAL_HANDSHAKE_BACKOFF_BASE,
    SERIAL_HANDSHAKE_BACKOFF_MAX,
)
from mcubridge.protocol import protocol, structures
from mcubridge.protocol.frame import Frame

if TYPE_CHECKING:
    from mcubridge.config.settings import RuntimeConfig
    from mcubridge.state.context import RuntimeState

logger = structlog.get_logger("mcubridge.serial")


class SerialTransport:
    """High-performance asyncio serial transport (Zero-Wrapper philosophy)."""

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
        self.service.register_serial_sender(self.send)

        self._stop_event = asyncio.Event()
        self._negotiating = False
        self._negotiation_future: asyncio.Future[bool] | None = None
        self._consecutive_crc_errors = 0
        self._tx_sequence_id = 0
        self.is_connected = False

    def _switch_local_baudrate(self, target_baud: int) -> None:
        if self.writer is None or self.writer.is_closing():
            raise RuntimeError("Cannot switch baudrate without active writer")

        try:
            # [SIL-2] Direct hardware access for baudrate switching
            serial_port = cast(Any, self.writer.transport).serial
            serial_port.baudrate = target_baud
            logger.info("Local UART switched to %d baud", target_baud)
        except (AttributeError, ValueError) as e:
            raise RuntimeError(f"UART access failed: {e}") from e

    async def run(self) -> None:
        """Main transport entry point with auto-reconnect."""
        self.loop = asyncio.get_running_loop()

        retryer = tenacity.AsyncRetrying(
            wait=tenacity.wait_exponential(multiplier=1, min=1, max=10),
            retry=tenacity.retry_if_not_exception_type(
                (asyncio.CancelledError, SerialHandshakeFatal)
            ),
            before_sleep=tenacity.before_sleep_log(logger, logging.WARNING),
            reraise=True,
        )

        while not self._stop_event.is_set():
            try:
                await retryer(self._connect_and_run)
            except asyncio.CancelledError:
                break
            except SerialHandshakeFatal as exc:
                logger.error("Fatal serial handshake error: %s", exc)
                raise
            except Exception as exc:
                logger.error("Transport fatal error: %s", exc)
                await asyncio.sleep(DEFAULT_RECONNECT_DELAY)

    async def _connect_and_run(self) -> None:
        """Single connection attempt and execution session."""
        logger.info("Connecting to MCU on %s...", self.config.serial_port)
        await self._toggle_dtr()

        connect_baud = self.config.serial_safe_baud or protocol.DEFAULT_SAFE_BAUDRATE

        self.reader, self.writer = await serial_asyncio_fast.open_serial_connection(
            url=self.config.serial_port,
            baudrate=connect_baud,
            xonxoff=False,
        )
        self.state.serial_writer = cast(asyncio.BaseTransport, self.writer)

        read_task = self.loop.create_task(self._read_loop(self.reader))
        self.is_connected = True

        try:
            if self.config.serial_baud != connect_baud:
                if not await self._negotiate_baudrate(self.config.serial_baud):
                    raise ConnectionError("Baudrate negotiation failed")

            await self.service.on_serial_connected()

            stop_task = self.loop.create_task(self._stop_event.wait())
            done, pending = await asyncio.wait(
                [read_task, stop_task],
                return_when=asyncio.FIRST_COMPLETED,
            )

            for task in pending:
                task.cancel()

            if read_task in done:
                exc = read_task.exception()
                if exc:
                    raise exc
                raise ConnectionError("Serial link EOF")

        finally:
            self.is_connected = False
            read_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await read_task
            try:
                await self.service.on_serial_disconnected()
            except Exception as e:
                logger.error("Error in disconnect hook: %s", e)
            if self.writer:
                self.writer.close()

    async def _toggle_dtr(self) -> None:
        """Hardware reset via DTR toggle using off-thread blocking pulse."""
        try:

            def _pulse():
                with serial.Serial(self.config.serial_port) as s:
                    s.dtr = False
                    time.sleep(0.1)
                    s.dtr = True

            await self.loop.run_in_executor(None, _pulse)  # type: ignore
        except (SerialException, OSError) as exc:
            logger.debug("DTR pulse failed: %s", exc)

    async def stop(self) -> None:
        """Gracefully stop the transport."""
        self._stop_event.set()
        if self.writer:
            self.writer.close()

    async def _read_loop(self, reader: asyncio.StreamReader) -> None:
        """Main packet reading loop delegating search to Python's C core."""
        while not self._stop_event.is_set():
            try:
                packet_with_sep = await reader.readuntil(protocol.FRAME_DELIMITER)
                packet_view = memoryview(packet_with_sep)[:-1]

                if not packet_view:
                    continue

                if logger.is_enabled_for(logging.DEBUG):
                    logger.debug(
                        "[SERIAL <- MCU] [RAW]: [%s]", packet_view.hex(" ").upper()
                    )

                # [SIL-2] Direct processing without task creation overhead
                await self._process_packet(packet_view)

            except asyncio.LimitOverrunError:
                logger.warning("Serial frame overflow, flushing.")
                self.state.serial_decode_errors += 1
                self.state.metrics.serial_decode_errors.inc()
                await reader.read(MAX_SERIAL_FRAME_BYTES)
            except asyncio.IncompleteReadError as e:
                logger.info(
                    "Serial EOF reached. Partial: %s",
                    e.partial.hex() if e.partial else "",
                )
                break
            except Exception as exc:
                logger.error("Read loop failure: %s", exc)
                break

    async def _process_packet(self, encoded_packet: bytes | memoryview) -> None:
        """Parse and dispatch a single serial frame (SIL-2)."""
        # 1. Baudrate Negotiation Hook
        if (
            self._negotiating
            and self._negotiation_future
            and not self._negotiation_future.done()
        ):
            try:
                frame = Frame.parse(cobs.decode(encoded_packet))
                if frame.command_id == protocol.Command.CMD_SET_BAUDRATE_RESP.value:
                    self._switch_local_baudrate(self.config.serial_baud)
                    self._negotiation_future.set_result(True)
                    return
            except Exception:
                pass

        # 2. Frame Parsing and Dispatch
        packet_bytes = (
            encoded_packet
            if isinstance(encoded_packet, bytes)
            else encoded_packet.tobytes()
        )
        try:
            decoded = cobs.decode(packet_bytes)
            frame = Frame.parse(decoded)

            if logger.is_enabled_for(logging.DEBUG):
                logger.debug(
                    "[MCU -> SERIAL] [SEQ:%04X] [RAW]: [%s]",
                    frame.sequence_id,
                    packet_bytes.hex(" ").upper(),
                )

            await self.service.handle_mcu_frame(
                frame.command_id, frame.sequence_id, frame.payload
            )

            # Metrics
            nbytes = len(encoded_packet)
            self.state.serial_bytes_received += nbytes
            self.state.serial_frames_received += 1
            self.state.metrics.serial_bytes_received.inc(nbytes)
            self.state.metrics.serial_frames_received.inc()
            self.state.serial_throughput_stats.record_rx(nbytes)

        except (cobs.DecodeError, ValueError) as exc:
            logger.warning("[SERIAL <- MCU] [MALFORMED]: %s", exc)
            self.state.serial_decode_errors += 1
            self.state.metrics.serial_decode_errors.inc()
            await self._check_baudrate_fallback()
        except Exception as exc:
            logger.error("[SERIAL <- MCU] [ERROR]: %s", exc)

    async def _check_baudrate_fallback(self) -> None:
        """CRC error monitoring for automatic baudrate fallback."""
        self._consecutive_crc_errors += 1
        if self._consecutive_crc_errors >= self.config.serial_fallback_threshold:
            logger.warning(
                "CRC threshold reached. Falling back to %d baud.",
                self.config.serial_safe_baud,
            )
            self._consecutive_crc_errors = 0
            if self.config.serial_baud != self.config.serial_safe_baud:
                await self._negotiate_baudrate(self.config.serial_safe_baud)

    async def send(self, cmd: int, payload: bytes, seq: int | None = None) -> bool:
        """Reliable serial frame delivery with flow control (XON/XOFF)."""
        if not self.writer or self.writer.is_closing():
            return False

        # [SIL-2] Flow Control Wait
        if not self.state.serial_tx_allowed.is_set():
            try:
                async with asyncio.timeout(30.0):
                    await self.state.serial_tx_allowed.wait()
            except (asyncio.TimeoutError, TimeoutError):
                raise ConnectionError("Serial flow control deadlock")
            except Exception:
                return False

        try:
            if seq is None:
                self._tx_sequence_id = (self._tx_sequence_id + 1) & protocol.UINT16_MAX
                seq = self._tx_sequence_id

            frame = Frame(command_id=cmd, sequence_id=seq, payload=payload)
            encoded = cobs.encode(frame.build()) + protocol.FRAME_DELIMITER

            if logger.is_enabled_for(logging.DEBUG):
                logger.debug(
                    "[SERIAL -> MCU] [SEQ:%04X] [RAW]: [%s]",
                    seq,
                    encoded.hex(" ").upper(),
                )

            self.writer.write(encoded)
            await self.writer.drain()

            # Metrics
            nbytes = len(encoded)
            self.state.serial_bytes_sent += nbytes
            self.state.serial_frames_sent += 1
            self.state.metrics.serial_bytes_sent.inc(nbytes)
            self.state.metrics.serial_frames_sent.inc()
            self.state.serial_throughput_stats.record_tx(nbytes)

            return True
        except Exception as e:
            logger.warning("Serial send failed: %s", e)
            return False

    async def _negotiate_baudrate(self, target_baud: int) -> bool:
        """Execute baudrate change protocol with the MCU."""
        logger.info("Negotiating baudrate: %d", target_baud)
        payload = msgspec.msgpack.encode(
            structures.SetBaudratePacket(baudrate=target_baud)
        )

        retryer = tenacity.AsyncRetrying(
            stop=tenacity.stop_after_attempt(3),
            wait=tenacity.wait_exponential(
                multiplier=SERIAL_HANDSHAKE_BACKOFF_BASE,
                max=SERIAL_HANDSHAKE_BACKOFF_MAX,
            ),
            retry=tenacity.retry_if_exception_type(asyncio.TimeoutError),
            reraise=True,
        )

        async def _attempt() -> bool:
            assert self.loop is not None
            self._negotiation_future = self.loop.create_future()
            if not await self.send(protocol.Command.CMD_SET_BAUDRATE.value, payload):
                raise asyncio.TimeoutError("Write failed")
            await asyncio.wait_for(
                self._negotiation_future, timeout=SERIAL_BAUDRATE_NEGOTIATION_TIMEOUT
            )
            return True

        self._negotiating = True
        try:
            return await retryer(_attempt)
        except Exception:
            return False
        finally:
            self._negotiating = False
            self._negotiation_future = None
