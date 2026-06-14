"""Periodic status writer for the MCU Bridge daemon."""

from __future__ import annotations

import asyncio
import structlog
import time
from pathlib import Path
from tempfile import NamedTemporaryFile
from typing import Any

import msgspec

from ..config.const import STATUS_FILE_PATH
from .context import RuntimeState

from google.protobuf.message import Message as ProtobufMessage

logger = structlog.get_logger("mcubridge.status")


def _enc_hook(obj: Any) -> Any:
    if isinstance(obj, ProtobufMessage):
        from google.protobuf.json_format import MessageToDict
        return MessageToDict(obj, preserving_proto_field_name=True)
    if hasattr(obj, "_pb") and isinstance(obj._pb, ProtobufMessage):
        from google.protobuf.json_format import MessageToDict
        return MessageToDict(obj._pb, preserving_proto_field_name=True)
    raise TypeError(f"Object of type {type(obj)} is not JSON serializable")


_json_enc = msgspec.json.Encoder(enc_hook=_enc_hook)
STATUS_FILE = Path(STATUS_FILE_PATH)


async def status_writer(state: RuntimeState, interval: int) -> None:
    """Persist lightweight status information periodically."""

    async def _write_tick() -> None:
        try:
            # [SIL-2] Simplified metrics
            child_stats: dict[str, Any] = {}

            payload = state.build_metrics_snapshot()
            payload["process_stats"] = child_stats
            payload["supervisors"] = {n: s for n, s in state.supervisor_stats.items()}
            payload["heartbeat_unix"] = time.time()

            write_task = asyncio.create_task(asyncio.to_thread(_write_status_file, payload))
            try:
                await asyncio.shield(write_task)
            except asyncio.CancelledError:
                await write_task
                raise
        except asyncio.CancelledError:
            raise
        except (OSError, RuntimeError, msgspec.MsgspecError) as e:
            logger.error("Periodic status write failed: %s", e)

    try:
        while True:
            await _write_tick()
            await asyncio.sleep(interval)
    except asyncio.CancelledError:
        logger.info("Status writer task cancelled.")
        raise


def _write_status_file(payload: dict[str, Any]) -> None:
    """[SIL-2] Atomic status persistence using zero-copy library primitives."""
    try:
        STATUS_FILE.parent.mkdir(parents=True, exist_ok=True)
        # [SIL-2] Use library encoder for atomic generation
        data = _json_enc.encode(payload)

        with NamedTemporaryFile("wb", dir=STATUS_FILE.parent, delete=False) as tf:
            tf.write(data)
            temp_name = tf.name
        Path(temp_name).replace(STATUS_FILE)
    except (msgspec.MsgspecError, OSError) as e:
        logger.error("Failed to write atomic status file: %s", e)
