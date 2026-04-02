"""Durable MQTT publish spool backed by persist-queue."""

from __future__ import annotations

import structlog
import time
from pathlib import Path
from typing import Any

import msgspec

from ..protocol.structures import QueuedPublish
from ..state.queues import PersistentQueue

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
        self._records = PersistentQueue[dict[str, Any]](
            directory=self.directory,
            max_items=self._limit,
        )
        self._dropped_due_to_limit = 0
        self._trim_events = 0
        self._last_trim_unix = 0.0
        self._corrupt_dropped = 0
        self._fallback_active = self._records.fallback_active
        self._failure_reason = self._records.fallback_reason
        self._last_error = self._records.last_error
        self._closed = False
        self._notify_fallback()

    def _notify_fallback(self) -> None:
        if not self._records.fallback_active or self._on_fallback is None or self._closed:
            return
        reason = self._records.fallback_reason or "initialization_failed"
        error_text = self._records.last_error
        original = RuntimeError(error_text) if error_text else None
        self._on_fallback(reason, original)

    def _refresh_fallback_state(self) -> None:
        self._fallback_active = self._records.fallback_active
        self._failure_reason = self._records.fallback_reason
        self._last_error = self._records.last_error
        self._closed = False
        self._notify_fallback()

    def __del__(self) -> None:
        # [SIL-2] Final safety check
        if not getattr(self, "_closed", True):
            self.close()

    def close(self) -> None:
        if getattr(self, "_closed", False):
            return
        self._closed = True
        try:
            if hasattr(self, "_records") and self._records:
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
        record = msgspec.structs.asdict(message.to_record())
        if not self._records.append(record):
            raise MQTTSpoolError("append_failed")
        self._refresh_fallback_state()

    def pop_next(self) -> QueuedPublish | None:
        while self.pending > 0 and not self._closed:
            record = self._records.popleft()
            self._refresh_fallback_state()
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
        record = msgspec.structs.asdict(message.to_record())
        if not self._records.appendleft(record):
            raise MQTTSpoolError("requeue_failed")
        self._refresh_fallback_state()

    @property
    def pending(self) -> int:
        return len(self._records)

    @property
    def is_degraded(self) -> bool:
        return self._fallback_active

    @property
    def failure_reason(self) -> str | None:
        return self._failure_reason

    @property
    def last_error(self) -> str | None:
        return self._last_error

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
