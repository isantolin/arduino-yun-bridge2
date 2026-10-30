"""Periodic status writer for the MCU Bridge daemon."""

from __future__ import annotations

import asyncio
import os
from pathlib import Path
from tempfile import NamedTemporaryFile

import structlog
from google.protobuf.json_format import MessageToJson
from google.protobuf.message import Message as ProtobufMessage

from .context import RuntimeState

logger = structlog.get_logger("mcubridge.status")
STATUS_FILE = Path(os.environ.get("MCUBRIDGE_STATUS_FILE") or "/tmp/mcubridge_status.json")


async def status_writer(state: RuntimeState, interval: int, status_file: Path | None = None) -> None:
    """Persist lightweight status information periodically."""
    target_file = status_file if status_file is not None else STATUS_FILE
    try:
        while True:
            try:
                # [SIL-2] Use BridgeStatus Protobuf for holistic snapshot
                status = state.build_status_snapshot()
                await asyncio.to_thread(write_status_file, status, target_file)
            except (OSError, RuntimeError, ValueError) as exc:
                logger.error("Periodic status write failed", error=str(exc))
            await asyncio.sleep(interval)
    except asyncio.CancelledError as exc:
        logger.info("Status writer task cancelled", error=str(exc))
        raise


def write_status_file(payload: ProtobufMessage, status_file: Path | None = None) -> bool:
    """[SIL-2] Atomic status persistence via Protobuf-native JSON serialization."""
    target_file = status_file if status_file is not None else STATUS_FILE
    try:
        target_file.parent.mkdir(parents=True, exist_ok=True)
        # [SIL-2] Direct Protobuf→JSON via library primitive (zero shim)
        data = MessageToJson(payload, preserving_proto_field_name=True).encode("utf-8")

        with NamedTemporaryFile("wb", dir=target_file.parent, delete=False) as tf:
            tf.write(data)
            temp_name = tf.name
        temp_path = Path(temp_name)
        temp_path.chmod(0o644)
        temp_path.replace(target_file)
        return True
    except (ValueError, OSError) as exc:
        logger.error("Failed to write atomic status file", error=str(exc))
        return False
