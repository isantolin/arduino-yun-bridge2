"""Periodic status writer for the Yun Bridge daemon."""
from __future__ import annotations

import asyncio
import json
import logging
import time
from pathlib import Path
from tempfile import NamedTemporaryFile
from typing import Any, Dict

from ..const import STATUS_FILE_PATH
from .context import RuntimeState

logger = logging.getLogger("yunbridge.status")
STATUS_FILE = Path(STATUS_FILE_PATH)


async def status_writer(state: RuntimeState, interval: int) -> None:
    """Persist lightweight status information periodically."""

    while True:
        try:
            serial_writer = state.serial_writer
            serial_connected = (
                serial_writer is not None and not serial_writer.is_closing()
            )
            payload: Dict[str, Any] = {
                "serial_connected": serial_connected,
                "mqtt_queue_size": state.mqtt_publish_queue.qsize(),
                "mqtt_queue_limit": state.mqtt_queue_limit,
                "mqtt_messages_dropped": state.mqtt_dropped_messages,
                "mqtt_drop_counts": dict(state.mqtt_drop_counts),
                "datastore_keys": list(state.datastore.keys()),
                "mailbox_size": len(state.mailbox_queue),
                "mailbox_bytes": state.mailbox_queue_bytes,
                "mcu_paused": state.mcu_is_paused,
                "console_queue_size": len(state.console_to_mcu_queue),
                "console_queue_bytes": state.console_queue_bytes,
                "running_processes": list(state.running_processes.keys()),
                "allowed_commands": list(state.allowed_commands),
                "link_synchronised": state.link_is_synchronized,
                "heartbeat_unix": time.time(),
                "mcu_version": (
                    {
                        "major": state.mcu_version[0],
                        "minor": state.mcu_version[1],
                    }
                    if state.mcu_version is not None
                    else None
                ),
            }
            await asyncio.to_thread(_write_status_file, payload)
        except asyncio.CancelledError:
            logger.info("Status writer task cancelled.")
            raise
        except Exception:
            logger.exception("Failed to write status file.")
        await asyncio.sleep(interval)


def cleanup_status_file() -> None:
    """Remove the status file if it exists."""

    try:
        STATUS_FILE.unlink(missing_ok=True)
    except OSError:
        logger.debug("Ignoring error while removing status file.")


def _write_status_file(payload: Dict[str, Any]) -> None:
    STATUS_FILE.parent.mkdir(parents=True, exist_ok=True)
    with NamedTemporaryFile(
        "w",
        encoding="utf-8",
        dir=STATUS_FILE.parent,
        delete=False,
    ) as handle:
        json.dump(payload, handle, indent=2)
        temp_name = handle.name
    Path(temp_name).replace(STATUS_FILE)
