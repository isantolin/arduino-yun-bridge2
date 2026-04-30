"""Periodic status writer for the MCU Bridge daemon."""

from __future__ import annotations

import asyncio
import structlog
import time
from pathlib import Path
from tempfile import NamedTemporaryFile
from typing import Any

import msgspec
import psutil

from ..config.const import STATUS_FILE_PATH
from ..protocol.structures import (
    ProcessStats,
)
from .context import RuntimeState

logger = structlog.get_logger("mcubridge.status")
_json_enc = msgspec.json.Encoder()
STATUS_FILE = Path(STATUS_FILE_PATH)


async def status_writer(state: RuntimeState, interval: int) -> None:
    """Persist lightweight status information periodically."""
    current_process = psutil.Process()

    async def _write_tick() -> None:
        try:
            # [SIL-2] Resource Monitoring Delegation to psutil
            child_stats: dict[str, ProcessStats] = {}
            try:
                for child in current_process.children(recursive=True):
                    try:
                        with child.oneshot():
                            child_stats[str(child.pid)] = ProcessStats(
                                name=child.name(),
                                cpu_percent=child.cpu_percent(),
                                memory_rss_bytes=child.memory_info().rss,
                            )
                    except (psutil.NoSuchProcess, psutil.AccessDenied):
                        continue
            except (psutil.NoSuchProcess, psutil.AccessDenied):
                pass

            payload = state.build_metrics_snapshot()
            payload["process_stats"] = child_stats
            payload["supervisors"] = {
                n: s.as_snapshot() for n, s in state.supervisor_stats.items()
            }
            payload["heartbeat_unix"] = time.time()

            write_task = asyncio.create_task(
                asyncio.to_thread(_write_status_file, payload)
            )
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
    STATUS_FILE.parent.mkdir(parents=True, exist_ok=True)

    try:
        # [SIL-2] Use library encoder for atomic generation
        data = _json_enc.encode(payload)

        with NamedTemporaryFile("wb", dir=STATUS_FILE.parent, delete=False) as tf:
            tf.write(data)
            temp_name = tf.name
        Path(temp_name).replace(STATUS_FILE)
    except (msgspec.MsgspecError, OSError) as e:
        logger.error("Failed to write atomic status file: %s", e)
