"""Durable spool for MQTT publish messages using msgspec and PersistentQueue."""

from __future__ import annotations

import logging
import time
from pathlib import Path
from typing import Any

import msgspec

from ..protocol.structures import QueuedPublish, SpoolRecord
from ..state.queues import PersistentQueue

logger = logging.getLogger("mcubridge.mqtt.spool")


class MQTTSpoolError(RuntimeError):
    """Raised when the filesystem spool cannot fulfill an operation."""

    def __init__(
        self,
        reason: str,
        *,
        original: BaseException | None = None,
    ) -> None:
        message = reason if original is None else f"{reason}:{original}"
        super().__init__(message)
        self.reason = reason
        self.original = original


class MQTTPublishSpool:
    """Hybrid spool that automates RAM/Disk management using PersistentQueue."""

    def __init__(
        self,
        directory: str,
        limit: int,
        *,
        on_fallback: Any | None = None,
    ) -> None:
        self.directory = Path(directory)

        # Fast storage (RAM) limit: 20% of total limit or 50 messages
        ram_limit = min(50, max(1, limit // 5))

        self._base = PersistentQueue[SpoolRecord](
            directory=self.directory,
            max_items=max(1, limit),
            ram_limit=ram_limit,
            encoder=msgspec.msgpack.encode,
            decoder=lambda b: msgspec.msgpack.decode(b, type=SpoolRecord),
        )

        self._dropped_due_to_limit = 0
        self._trim_events = 0
        self._last_trim_unix = 0.0
        self._corrupt_dropped = 0

    def close(self) -> None:
        self._base.clear()

    def append(self, message: QueuedPublish) -> None:
        record: SpoolRecord = message.to_record()
        # [SIL-2] The PersistentQueue base handles LRU-style dropping if limit is reached.
        # We wrap it to track our specific metrics.
        pre_len = len(self._base)
        if self.limit and pre_len >= self.limit:
            self._dropped_due_to_limit += 1
            self._trim_events += 1
            self._last_trim_unix = time.time()

        if not self._base.append(record):
            logger.error("Failed to append to MQTT spool")

    def pop_next(self) -> QueuedPublish | None:
        record = self._base.popleft()
        if record:
            return QueuedPublish.from_record(record)
        return None

    def requeue(self, message: QueuedPublish) -> None:
        """Push message back to the front of the queue."""
        record: SpoolRecord = message.to_record()
        self._base.appendleft(record)

    @property
    def pending(self) -> int:
        return len(self._base)

    @property
    def is_degraded(self) -> bool:
        # Degradation is now implicit if directory setup failed (Base uses RAM only)
        return self._base.directory is None

    @property
    def limit(self) -> int:
        return self._base.max_items or 0

    @limit.setter
    def limit(self, value: int) -> None:
        self._base.max_items = value

    def snapshot(self) -> dict[str, int | float]:
        return {
            "pending": self.pending,
            "limit": self.limit,
            "dropped_due_to_limit": self._dropped_due_to_limit,
            "trim_events": self._trim_events,
            "last_trim_unix": self._last_trim_unix,
            "corrupt_dropped": self._corrupt_dropped,
            "fallback_active": 1 if self.is_degraded else 0,
        }


__all__ = ["QueuedPublish", "MQTTPublishSpool", "MQTTSpoolError"]
