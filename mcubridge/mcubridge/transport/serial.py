"""Serial transport implementation using pyserial-asyncio-fast Streams.

This module implements a Zero-Overhead asyncio transport using StreamReader
and StreamWriter. It delegates delimiter searching to Python's C core via
`readuntil(b"\0")`.
"""

from __future__ import annotations

import asyncio
import logging
from typing import TYPE_CHECKING, Any, cast

from cobs import cobs
import serial
import structlog
import tenacity
from transitions import Machine

from mcubridge.config.const import (
    DEFAULT_BAUDRATE,
    DEFAULT_RECONNECT_DELAY,
    DEFAULT_SAFE_BAUDRATE,
    SERIAL_BAUDRATE_NEGOTIATION_TIMEOUT,
    SERIAL_HANDSHAKE_BACKOFF_BASE,
    SERIAL_HANDSHAKE_BACKOFF_MAX,
)
from mcubridge.protocol import protocol, structures
from mcubridge.protocol.frame import Frame

if TYPE_CHECKING:
    from mcubridge.config.settings import RuntimeConfig
    from mcubridge.services.runtime import BridgeService
    from mcubridge.state.context import RuntimeState

logger = structlog.get_logger("mcubridge.serial")


class SerialTransport:
    """Asyncio-based serial transport with automated reconnection (SIL-2)."""

    STATE_DISCONNECTED = "disconnected"
    STATE_NEGOTIATING = "negotiating"
    STATE_CONNECTED = "connected"

    def __init__(
        self, config: RuntimeConfig, state: RuntimeState, service: BridgeService
    ) -> None:
        self.config = config
        self.state = state
        self.service = service
        self.baudrate = DEFAULT_SAFE_BAUDRATE
        self.reader: asyncio.StreamReader | None = None
        self.writer: asyncio.StreamWriter | None = None
        self.loop: asyncio.AbstractEventLoop | None = None
        self._stop_event = asyncio.Event()

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
        """Signal the transport to stop and close connection."""
        self._stop_event.set()
        await self._close_connection()

    async def _retryable_run(self, loop: asyncio.AbstractEventLoop) -> None:
        """Handle a single connection lifecycle with auto-negotiation."""
        self.baudrate = DEFAULT_SAFE_BAUDRATE
        logger.info(
            "Opening serial port %s at %d baud...",
            self.config.serial_port,
            self.baudrate,
        )

        from serial_asyncio_fast import open_serial_connection

        self.reader, self.writer = await open_serial_connection(
            url=self.config.serial_port,
            baudrate=self.baudrate,
            loop=loop,
        )

        try:
            self.service.register_serial_sender(self._serial_sender)
            await self.service.on_serial_connected()

            # Attempt to switch to high-speed baudrate if configured
            if self.config.serial_baud != self.baudrate:
                if await self._negotiate_baudrate(self.config.serial_baud):
                    logger.info("Serial high-speed link established.")
                else:
                    logger.warning("Baudrate negotiation failed; staying at safe baud.")

            # Main read loop
            await self._read_loop()

        finally:
            await self._close_connection()
            try:
                await self.service.on_serial_disconnected()
            except (OSError, ValueError, RuntimeError) as e:
                logger.error("Error in on_serial_disconnected hook: %s", e)

    async def _read_loop(self) -> None:
        """Low-level delimiter-based read loop (Zero-Heap friendly)."""
        assert self.reader is not None
        while not self._stop_event.is_set():
            try:
                # [SIL-2] readuntil(0x00) is highly efficient as it uses
                # buffer searching in the C core of asyncio.
                packet = await self.reader.readuntil(protocol.FRAME_DELIMITER)
                if packet:
                    await self._process_packet(packet)
            except (asyncio.LimitOverrunError, ValueError) as e:
                logger.warning("Serial read frame too large or malformed: %s", e)
                # Recover by consuming until next delimiter
                await self.reader.read(64)
            except asyncio.IncompleteReadError:
                logger.info("Serial port closed by peer.")
                break

    async def _process_packet(self, packet: bytes) -> None:
        """Parse and dispatch an inbound serial frame."""
        try:
            # [SIL-2] Strip delimiter and decode COBS
            raw_data = cobs.decode(packet[:-1])
            cmd_id, seq_id, payload = Frame.parse(raw_data)
            await self.service.handle_mcu_frame(cmd_id, seq_id, payload)
        except (ValueError, cobs.DecodeError) as e:
            # Handle CRC mismatches and malformed frames
            error_text = str(e)
            if "checksum" in error_text.lower() or "crc" in error_text.lower():
                self.state.record_serial_crc_error()
                logger.warning("MCU > CRC error: %s", error_text)
            else:
                logger.warning("MCU > Malformed frame: %s", error_text)

    async def _serial_sender(self, command_id: int, payload: bytes, seq_id: int = 0) -> bool:
        """Internal low-level writer for the Flow Controller."""
        if self.writer is None or self.writer.is_closing():
            return False

        try:
            frame = Frame(
                command_id=command_id,
                sequence_id=seq_id,
                payload=payload,
            ).build()

            if logger.isEnabledFor(logging.DEBUG):
                logger.log(
                    logging.DEBUG,
                    "SERIAL TX > 0x%02X (seq=%d) [%s]",
                    command_id,
                    seq_id,
                    frame.hex(" ").upper(),
                )

            self.writer.write(frame)
            await self.writer.drain()
            return True
        except (OSError, asyncio.CancelledError) as e:
            logger.warning("Send failed: %s", e)
            return False

    async def _negotiate_baudrate(self, target_baud: int) -> bool:
        """Switch to target baudrate with MCU handshake (SIL-2)."""
        if target_baud == self.baudrate:
            return True

        logger.info("Negotiating baudrate switch to %d...", target_baud)
        self.begin_negotiate()

        # Send negotiation command via flow controller
        success = await self.service.serial_flow.negotiate_baudrate(target_baud)

        if success:
            try:
                self._switch_local_baudrate(target_baud)
                self.baudrate = target_baud
                self.mark_connected()
                return True
            except RuntimeError as e:
                logger.error("Failed to switch local baudrate: %s", e)

        self.mark_disconnected()
        return False

    async def _close_connection(self) -> None:
        """Gracefully close the serial connection."""
        self.mark_disconnected()
        if self.writer:
            try:
                self.writer.close()
                await self.writer.wait_closed()
            except (OSError, asyncio.CancelledError):
                pass
            self.writer = None
            self.reader = None
