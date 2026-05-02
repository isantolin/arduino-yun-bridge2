"""Low-level serial link management (Zero-Wrapper)."""

from __future__ import annotations

import asyncio
import logging
import structlog
import time
from typing import Any, cast, Callable

import msgspec
from cobs import cobs
import serial
import serial_asyncio_fast
import tenacity

from mcubridge.config.const import (
    MAX_SERIAL_FRAME_BYTES,
    SERIAL_BAUDRATE_NEGOTIATION_TIMEOUT,
    SERIAL_HANDSHAKE_BACKOFF_BASE,
    SERIAL_HANDSHAKE_BACKOFF_MAX,
)
from mcubridge.protocol import protocol, structures
from mcubridge.protocol.frame import Frame
from mcubridge.state.context import RuntimeState

logger = structlog.get_logger("mcubridge.serial.link")


async def open_serial_link(
    port: str, baudrate: int
) -> tuple[asyncio.StreamReader, asyncio.StreamWriter]:
    """Open a serial connection using pyserial-asyncio-fast."""
    reader, writer = await serial_asyncio_fast.open_serial_connection(
        url=port,
        baudrate=baudrate,
        xonxoff=False,
    )
    return reader, writer


async def toggle_dtr(port: str) -> None:
    """Hardware reset via DTR toggle."""
    try:

        def _pulse() -> None:
            with serial.Serial(port) as s:
                s.dtr = False
                time.sleep(0.1)
                s.dtr = True

        await asyncio.get_running_loop().run_in_executor(None, _pulse)
    except (serial.SerialException, OSError) as exc:
        logger.debug("DTR toggle failed: %s", exc)


def switch_local_baudrate(writer: asyncio.StreamWriter, baudrate: int) -> None:
    """Switch the local UART baudrate by accessing the underlying serial object."""
    try:
        serial_port = cast(Any, writer.transport).serial
        serial_port.baudrate = baudrate
        logger.info("Local UART switched to %d baud", baudrate)
    except (AttributeError, ValueError) as e:
        raise RuntimeError(
            f"Serial transport does not expose the underlying UART: {e}"
        ) from e


async def write_frame(
    writer: asyncio.StreamWriter,
    state: RuntimeState,
    command_id: int,
    payload: bytes,
    sequence_id: int | None = None,
) -> bool:
    """Encode and write a single COBS-framed RPC packet to the wire."""
    if writer.is_closing():
        return False

    if sequence_id is None:
        state.tx_sequence_id = (state.tx_sequence_id + 1) & protocol.UINT16_MAX
        sequence_id = state.tx_sequence_id

    try:
        frame = Frame(command_id=command_id, sequence_id=sequence_id, payload=payload)
        encoded = cobs.encode(frame.build()) + protocol.FRAME_DELIMITER

        if logger.is_enabled_for(logging.DEBUG):
            logger.debug(
                "[SERIAL -> MCU] [SEQ:%04X] [RAW]: [%s]",
                sequence_id,
                encoded.hex(" ").upper(),
            )

        writer.write(encoded)
        await writer.drain()

        # Update metrics
        nbytes = len(encoded)
        state.serial_bytes_sent += nbytes
        state.serial_frames_sent += 1
        state.metrics.serial_bytes_sent.inc(nbytes)
        state.metrics.serial_frames_sent.inc()
        state.serial_throughput_stats.record_tx(nbytes)
        return True
    except (OSError, asyncio.CancelledError) as e:
        logger.warning("Serial write failed: %s", e)
        return False


async def negotiate_baudrate(
    writer: asyncio.StreamWriter,
    state: RuntimeState,
    target_baud: int,
    negotiation_future: asyncio.Future[bool],
) -> bool:
    """Execute the baudrate switch protocol with the MCU."""
    logger.info("Negotiating baudrate switch to %d...", target_baud)

    payload = msgspec.msgpack.encode(structures.SetBaudratePacket(baudrate=target_baud))

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

    async def _attempt() -> bool:
        if not await write_frame(
            writer, state, protocol.Command.CMD_SET_BAUDRATE.value, payload
        ):
            raise asyncio.TimeoutError("Write failed")

        try:
            await asyncio.wait_for(
                negotiation_future,
                timeout=SERIAL_BAUDRATE_NEGOTIATION_TIMEOUT,
            )
            return True
        except asyncio.TimeoutError:
            raise

    try:
        return await retryer(_attempt)
    except (tenacity.RetryError, asyncio.TimeoutError):
        return False


async def read_loop(
    reader: asyncio.StreamReader,
    state: RuntimeState,
    service: Any,
    stop_event: asyncio.Event,
    on_packet: Callable[[bytes | memoryview], None],
) -> None:
    """Continuously read COBS frames from the serial reader."""
    while not stop_event.is_set():
        try:
            packet_with_sep = await reader.readuntil(protocol.FRAME_DELIMITER)
            packet_view = memoryview(packet_with_sep)[:-1]

            if packet_view:
                if logger.is_enabled_for(logging.DEBUG):
                    logger.debug(
                        "[SERIAL <- MCU] [RAW]: [%s]", packet_view.hex(" ").upper()
                    )
                on_packet(packet_view)

        except asyncio.LimitOverrunError:
            logger.warning("Serial packet too large, flushing.")
            state.serial_decode_errors += 1
            state.metrics.serial_decode_errors.inc()
            await reader.read(MAX_SERIAL_FRAME_BYTES)
        except asyncio.IncompleteReadError as e:
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
            logger.error("Error in serial read loop: %s", exc)
            break
