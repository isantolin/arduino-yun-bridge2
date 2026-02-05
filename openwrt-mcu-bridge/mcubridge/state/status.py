"""Periodic status writer for the MCU Bridge daemon."""

from __future__ import annotations

import asyncio
import msgspec
import logging
import time
from pathlib import Path
from tempfile import NamedTemporaryFile
from typing import Any

from ..const import STATUS_FILE_PATH
from .context import RuntimeState

logger = logging.getLogger("mcubridge.status")
STATUS_FILE = Path(STATUS_FILE_PATH)


async def status_writer(state: RuntimeState, interval: int) -> None:
    """Persist lightweight status information periodically."""

    while True:
        try:
            serial_writer = state.serial_writer
            serial_connected = serial_writer is not None and not serial_writer.is_closing()
            payload: dict[str, Any] = {
                "serial_connected": serial_connected,
                "mqtt_queue_size": state.mqtt_publish_queue.qsize(),
                "mqtt_queue_limit": state.mqtt_queue_limit,
                "mqtt_messages_dropped": state.mqtt_dropped_messages,
                "mqtt_drop_counts": dict(state.mqtt_drop_counts),
                "mqtt_spooled_messages": state.mqtt_spooled_messages,
                "mqtt_spooled_replayed": state.mqtt_spooled_replayed,
                "mqtt_spool_errors": state.mqtt_spool_errors,
                "mqtt_spool_degraded": state.mqtt_spool_degraded,
                "mqtt_spool_failure_reason": (state.mqtt_spool_failure_reason),
                "mqtt_spool_retry_attempts": (state.mqtt_spool_retry_attempts),
                "mqtt_spool_backoff_until": (state.mqtt_spool_backoff_until),
                "mqtt_spool_last_error": state.mqtt_spool_last_error,
                "mqtt_spool_recoveries": state.mqtt_spool_recoveries,
                "mqtt_spool_pending": (state.mqtt_spool.pending if state.mqtt_spool is not None else 0),
                "file_storage_root": state.file_system_root,
                "file_storage_bytes_used": state.file_storage_bytes_used,
                "file_storage_quota_bytes": state.file_storage_quota_bytes,
                "file_write_max_bytes": state.file_write_max_bytes,
                "file_write_limit_rejections": (state.file_write_limit_rejections),
                "file_storage_limit_rejections": (state.file_storage_limit_rejections),
                "datastore_keys": list(state.datastore.keys()),
                "mailbox_size": len(state.mailbox_queue),
                "mailbox_bytes": state.mailbox_queue_bytes,
                "mailbox_dropped_messages": state.mailbox_dropped_messages,
                "mailbox_dropped_bytes": state.mailbox_dropped_bytes,
                "mailbox_truncated_messages": state.mailbox_truncated_messages,
                "mailbox_truncated_bytes": state.mailbox_truncated_bytes,
                "mailbox_incoming_dropped_messages": (state.mailbox_incoming_dropped_messages),
                "mailbox_incoming_dropped_bytes": (state.mailbox_incoming_dropped_bytes),
                "mailbox_incoming_truncated_messages": (state.mailbox_incoming_truncated_messages),
                "mailbox_incoming_truncated_bytes": (state.mailbox_incoming_truncated_bytes),
                "mcu_paused": state.mcu_is_paused,
                "console_queue_size": len(state.console_to_mcu_queue),
                "console_queue_bytes": state.console_queue_bytes,
                "console_dropped_chunks": state.console_dropped_chunks,
                "console_dropped_bytes": state.console_dropped_bytes,
                "console_truncated_chunks": state.console_truncated_chunks,
                "console_truncated_bytes": state.console_truncated_bytes,
                "watchdog_enabled": state.watchdog_enabled,
                "watchdog_interval": state.watchdog_interval,
                "watchdog_beats": state.watchdog_beats,
                "watchdog_last_beat": state.last_watchdog_beat,
                "running_processes": list(state.running_processes.keys()),
                "allowed_commands": list(state.allowed_commands),
                "config_source": state.config_source,
                "link_synchronised": state.link_is_synchronized,
                "handshake_attempts": state.handshake_attempts,
                "handshake_successes": state.handshake_successes,
                "handshake_failures": state.handshake_failures,
                "handshake_last_error": state.last_handshake_error,
                "handshake_last_unix": state.last_handshake_unix,
                "bridge": state.build_bridge_snapshot(),
                "serial_flow": state.serial_flow_stats.as_dict(),
                "supervisors": {name: stats.as_dict() for name, stats in state.supervisor_stats.items()},
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
            write_task = asyncio.create_task(asyncio.to_thread(_write_status_file, payload))
            try:
                await asyncio.shield(write_task)
            except asyncio.CancelledError:
                await write_task
                raise
        except asyncio.CancelledError:
            logger.info("Status writer task cancelled.")
            raise
        await asyncio.sleep(interval)


def cleanup_status_file() -> None:
    """Remove the status file if it exists."""

    try:
        STATUS_FILE.unlink(missing_ok=True)
    except OSError:
        logger.debug("Ignoring error while removing status file.")


def _write_status_file(payload: dict[str, Any]) -> None:
    STATUS_FILE.parent.mkdir(parents=True, exist_ok=True)
    with NamedTemporaryFile(
        "wb",
        dir=STATUS_FILE.parent,
        delete=False,
    ) as handle:
        handle.write(msgspec.json.encode(payload))
        temp_name = handle.name
    Path(temp_name).replace(STATUS_FILE)
