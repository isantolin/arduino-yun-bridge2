"""Durable MQTT publish spool backed by diskcache."""

from __future__ import annotations

import structlog
import time
from pathlib import Path
from typing import Any

import msgspec

from ..protocol.structures import QueuedPublish
from ..state.queues import BridgeQueue

logger = structlog.get_logger("mcubridge.mqtt.spool")


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
    """MQTT spool with durable FIFO persistence under /tmp."""

    def __init__(
        self,
        directory: str,
        limit: int,
        *,
        on_fallback: Any | None = None,
    ) -> None:
        self.directory = Path(directory)
        self._on_fallback = on_fallback
        self._limit = max(1, limit)
        self._records = BridgeQueue[dict[str, Any]](
            directory=directory,
            max_items=limit,
        )
        self._dropped_due_to_limit = 0
        self._trim_events = 0
        self._last_trim_unix = 0.0
        self._corrupt_dropped = 0
        self._closed = False

    def close(self) -> None:
        if getattr(self, "_closed", False):
            return
        self._closed = True
        try:
            if hasattr(self, "_records"):
                self._records.close()
        finally:
            if hasattr(self, "_records"):
                del self._records

    def append(self, message: QueuedPublish) -> None:
        if self._closed:
            return
        if self.pending >= self._limit:
            self._dropped_due_to_limit += 1
            self._trim_events += 1
            self._last_trim_unix = time.time()
        record = msgspec.structs.asdict(message)
        evt = self._records.append(record)
        if not evt.success:
            raise MQTTSpoolError("append_failed")

    def pop_next(self) -> QueuedPublish | None:
        while self.pending > 0 and not self._closed:
            record = self._records.popleft()
            if record is None:
                return None
            try:
                return QueuedPublish.from_record(record)
            except (msgspec.MsgspecError, TypeError, ValueError) as exc:
                self._corrupt_dropped += 1
                logger.warning("Dropping corrupt MQTT spool entry: %s", exc)
        return None

    def requeue(self, message: QueuedPublish) -> None:
        if self._closed:
            return
        record = msgspec.structs.asdict(message)
        self._records.appendleft(record)

    @property
    def pending(self) -> int:
        return len(self._records)

    @property
    def is_degraded(self) -> bool:
        """Return True if the spool is operating in RAM-only mode."""
        return self._records.fallback_active

    @property
    def failure_reason(self) -> str | None:
        return None

    @property
    def last_error(self) -> str | None:
        """Return the last error message from the underlying queue."""
        return self._records.last_error

    @property
    def limit(self) -> int:
        return self._limit

    @limit.setter
    def limit(self, value: int) -> None:
        self._limit = max(1, value)
        self._records.max_items = self._limit

    def snapshot(self) -> dict[str, int | float]:
        stats = {
            "pending": self.pending,
            "limit": self.limit,
            "dropped_due_to_limit": self._dropped_due_to_limit,
            "trim_events": self._trim_events,
            "last_trim_unix": self._last_trim_unix,
            "corrupt_dropped": self._corrupt_dropped,
            "fallback_active": int(self.is_degraded),
        }
        return stats


__all__ = ["QueuedPublish", "MQTTPublishSpool", "MQTTSpoolError"]
