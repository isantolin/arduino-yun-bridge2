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
from typing import TYPE_CHECKING, Any, Final

from cobs.cobs import encode as cobs_encode, decode as cobs_decode, DecodeError as CobsDecodeError
import serial
import serial_asyncio_fast
import tenacity

from mcubridge.config.const import (
    SERIAL_HANDSHAKE_BACKOFF_BASE,
    SERIAL_HANDSHAKE_BACKOFF_MAX,
)
from mcubridge.config.settings import RuntimeConfig
from mcubridge.protocol import protocol
from mcubridge.state.context import RuntimeState
from mcubridge.router.routers import MCUHandlerRegistry, MQTTRouter

if TYPE_CHECKING:
    from collections.abc import Callable

logger = logging.getLogger("mcubridge.transport.serial")


def _is_binary_packet(packet: bytes) -> bool:
    """Validate packet header matches protocol v2."""
    if len(packet) < 5:
        return False
    return packet[0] == protocol.PROTOCOL_VERSION


def _format_hex(data: bytes) -> str:
    """Format binary data as a structured hex string [AA BB CC]."""
    return f"[{' '.join(f'{b:02X}' for b in data)}]"


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

        # [SIL-2] Pre-allocated registries
        self.registry = MCUHandlerRegistry()
        self.router = MQTTRouter()

        # Register ourselves as the sender for the service
        self.service.register_serial_sender(self._serial_sender)

        self._stop_event = asyncio.Event()
        self._negotiating = False
        self._negotiation_future: asyncio.Future[bool] | None = None
        self._on_frame_callback: Callable[[int, bytes], None] | None = None

        # State Machine (Simplified for SIL-2 reliability)
        self.fsm_state: str = self.STATE_DISCONNECTED

    def _set_state(self, new_state: str) -> None:
        if self.fsm_state != new_state:
            old_state = self.fsm_state
            self.fsm_state = new_state
            logger.info("Serial transport state transition: %s -> %s", old_state, new_state)

    def begin_negotiate(self) -> None:
        self._set_state(self.STATE_NEGOTIATING)

    def mark_connected(self) -> None:
        self._set_state(self.STATE_CONNECTED)

    def mark_disconnected(self) -> None:
        self._set_state(self.STATE_DISCONNECTED)

    def set_on_frame_callback(self, callback: Callable[[int, bytes], None]) -> None:
        self._on_frame_callback = callback

    async def __aenter__(self) -> SerialTransport:
        await self.connect()
        return self

    async def __aexit__(self, exc_type: Any, exc_val: Any, exc_tb: Any) -> None:
        await self.disconnect()

    async def connect(self) -> bool:
        """Establish serial connection with retries."""
        if self.fsm_state != self.STATE_DISCONNECTED:
            return True

        self.begin_negotiate()
        retryer = tenacity.AsyncRetrying(
            stop=tenacity.stop_after_attempt(5),
            wait=tenacity.wait_exponential(multiplier=SERIAL_HANDSHAKE_BACKOFF_BASE, max=SERIAL_HANDSHAKE_BACKOFF_MAX),
            retry=tenacity.retry_if_exception_type((OSError, serial.SerialException)),
            reraise=False,
        )

        try:
            async for attempt in retryer:
                with attempt:
                    await self._establish_connection()
            return True
        except tenacity.RetryError:
            self.mark_disconnected()
            return False

    async def _establish_connection(self) -> None:
        logger.info("Opening serial port: %s@%d", self.config.serial_port, self.config.serial_baud)

        self.reader, self.writer = await serial_asyncio_fast.open_serial_connection(
            url=self.config.serial_port,
            baudrate=self.config.serial_baud,
        )
        self.mark_connected()
        self._stop_event.clear()
        asyncio.create_task(self._read_loop())

    async def disconnect(self) -> None:
        """Orderly close of serial connection."""
        self._stop_event.set()
        if self.writer:
            self.writer.close()
            with contextlib.suppress(Exception):
                await self.writer.wait_closed()
        self.mark_disconnected()
        self.reader = None
        self.writer = None

    async def write_frame(self, command_id: int, payload: bytes) -> bool:
        """Writes a COBS-encoded frame to the serial port."""
        if not self.writer or self.fsm_state != self.STATE_CONNECTED:
            logger.warning(
                "Attempted to write frame 0x%02X while not connected (State: %s, Writer: %s)",
                command_id, self.fsm_state, "Present" if self.writer else "None"
            )
            return False

        logger.debug("Writing frame 0x%02X len %d", command_id, len(payload))

        # [SIL-2] Construct binary frame
        # Version(1) + Length(2) + Command(2) + Payload(N) + CRC(4)
        frame = bytearray([protocol.PROTOCOL_VERSION])
        frame.extend(len(payload).to_bytes(2, "big"))
        frame.extend(command_id.to_bytes(2, "big"))
        frame.extend(payload)

        crc = protocol.calculate_crc32(bytes(frame))
        frame.extend(crc.to_bytes(4, "big"))

        # COBS encode
        encoded = cobs_encode(bytes(frame)) + protocol.FRAME_DELIMITER

        try:
            self.writer.write(encoded)
            await self.writer.drain()
            logger.debug("[SERIAL -> MCU] %s", _format_hex(bytes(frame)))
            return True
        except (OSError, serial.SerialException) as exc:

            logger.warning("Write failed: %s", exc)
            asyncio.create_task(self.disconnect())
            return False

    async def _read_loop(self) -> None:
        """Infinite read loop processing delimited COBS frames."""
        while not self._stop_event.is_set() and self.reader:
            try:
                # [OPTIMIZATION] Python C-core delimiter search
                packet = await self.reader.readuntil(protocol.FRAME_DELIMITER)
                if not packet:
                    continue

                # Strip delimiter and decode
                raw_payload = packet[:-1]
                if not raw_payload:
                    continue

                try:
                    decoded = cobs_decode(raw_payload)
                except CobsDecodeError:
                    logger.warning("COBS decode error for packet: %s", _format_hex(raw_payload))
                    continue

                if not _is_binary_packet(decoded):
                    # Maybe it's a log line?
                    try:
                        line = decoded.decode("utf-8", errors="ignore").strip()
                        if line:
                            logger.info("[MCU] %s", line)
                    except Exception:
                        pass
                    continue

                # Parse frame
                if len(decoded) < 9:
                    continue

                # CRC Check
                payload_len = int.from_bytes(decoded[1:3], "big")
                if len(decoded) != 9 + payload_len:
                    continue

                # Check CRC
                computed_crc = protocol.calculate_crc32(decoded[:-4])
                received_crc = int.from_bytes(decoded[-4:], "big")
                if computed_crc != received_crc:
                    logger.warning("CRC mismatch on received frame: %s", _format_hex(decoded))
                    continue

                # Dispatch
                cmd_id = int.from_bytes(decoded[3:5], "big")
                payload = bytes(decoded[5 : 5 + payload_len])

                logger.debug("[MCU -> SERIAL] %s", _format_hex(decoded))

                if self._on_frame_callback:
                    self._on_frame_callback(cmd_id, payload)
            except Exception as exc:
                if not self._stop_event.is_set():
                    logger.warning("Read loop error: %s", exc)
                    asyncio.create_task(self.disconnect())
                break

    async def _serial_sender(self, cmd: int, payload: bytes) -> bool:
        """Internal adapter for BridgeContext compatibility."""
        return await self.write_frame(cmd, payload)
