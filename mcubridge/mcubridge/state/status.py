"""Periodic status writer for the MCU Bridge daemon."""

from __future__ import annotations

import asyncio
import structlog
from pathlib import Path
from tempfile import NamedTemporaryFile

from google.protobuf.json_format import MessageToJson
from google.protobuf.message import Message as ProtobufMessage

from ..config.const import STATUS_FILE_PATH
from .context import RuntimeState

logger = structlog.get_logger("mcubridge.status")

STATUS_FILE = Path(STATUS_FILE_PATH)


async def status_writer(state: RuntimeState, interval: int) -> None:
    """Persist lightweight status information periodically."""

    async def _write_tick() -> None:
        try:
            # [SIL-2] Use BridgeStatus Protobuf for holistic snapshot
            status = state.build_status_snapshot()

            write_task = asyncio.create_task(asyncio.to_thread(_write_status_file, status))
            try:
                await asyncio.shield(write_task)
            except asyncio.CancelledError:
                await write_task
                raise
        except asyncio.CancelledError:
            raise
        except (OSError, RuntimeError, ValueError) as e:
            logger.error("Periodic status write failed: %s", e)

    try:
        while True:
            await _write_tick()
            await asyncio.sleep(interval)
    except asyncio.CancelledError:
        logger.info("Status writer task cancelled.")
        raise


def _write_status_file(payload: ProtobufMessage) -> None:
    """[SIL-2] Atomic status persistence via Protobuf-native JSON serialization."""
    try:
        STATUS_FILE.parent.mkdir(parents=True, exist_ok=True)
        # [SIL-2] Direct Protobuf→JSON via library primitive (zero shim)
        data = MessageToJson(payload, preserving_proto_field_name=True).encode("utf-8")

        with NamedTemporaryFile("wb", dir=STATUS_FILE.parent, delete=False) as tf:
            tf.write(data)
            temp_name = tf.name
        Path(temp_name).replace(STATUS_FILE)
    except (ValueError, OSError) as e:
        logger.error("Failed to write atomic status file: %s", e)
