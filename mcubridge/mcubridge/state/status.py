"""Periodic status reporting task for the MCU Bridge daemon."""

from __future__ import annotations

import asyncio
import logging
import os
from typing import TYPE_CHECKING, Any

import msgspec
from mcubridge.protocol.topics import Topic

if TYPE_CHECKING:
    from mcubridge.state.context import RuntimeState

logger = logging.getLogger("mcubridge.status")


async def publish_status_periodically(
    state: RuntimeState,
    interval: float,
    publish_callback: Any,
) -> None:
    """Task to periodically publish bridge state snapshots to MQTT."""
    if interval <= 0:
        return

    topic = f"{state.mqtt_topic_prefix}/{Topic.STATUS.value}/summary"

    while True:
        await asyncio.sleep(interval)
        try:
            snapshot = state.capture_snapshot()
            payload = msgspec.json.encode(snapshot)
            await publish_callback(topic, payload)
            logger.debug("Published bridge status snapshot to %s", topic)
        except Exception as exc:
            logger.error("Failed to publish bridge status: %s", exc)


async def status_writer(state: RuntimeState, interval: float) -> None:
    """Periodic task to write bridge status to a local file for LuCI."""
    # Implementation of writing to /tmp/mcubridge_status.json
    pass


def cleanup_status_file() -> None:
    """Remove the status file on shutdown."""
    from mcubridge.config.const import STATUS_FILE_PATH
    if os.path.exists(STATUS_FILE_PATH):
        try:
            os.remove(STATUS_FILE_PATH)
        except OSError:
            pass


def build_legacy_status_payload(state: RuntimeState) -> dict[str, Any]:
    """Build a legacy dictionary-based status payload for backward compatibility."""
    mcu_mem = 0
    try:
        # Accessing internal for typechecking purposes in this recovery phase
        mcu_mem = int(state.metrics.mcu_free_memory._value.get()) # type: ignore
    except (AttributeError, TypeError):
        pass

    return {
        "serial": {
            "connected": state.is_connected,
            "synchronized": state.is_synchronized,
        },
        "mqtt": {
            "queue_size": state.mqtt_publish_queue.qsize(),
            "dropped": state.mqtt_dropped_messages,
        },
        "resources": {
            "memory_free": mcu_mem,
        }
    }
