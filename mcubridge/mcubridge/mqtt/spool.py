"""Durable MQTT publish spool backed by diskcache."""

from __future__ import annotations

import sqlite3
from collections import deque
from typing import Any

import diskcache
import msgspec
import structlog

from ..protocol.structures import QueuedPublish

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
        self._on_fallback = on_fallback
        self.limit = limit
        self._corrupt_dropped = 0
        self._dropped_due_to_limit = 0
        self._directory = directory

        try:
            self._cache = diskcache.Cache(directory)  # type: ignore
            self._deque = diskcache.Deque.fromcache(self._cache)  # type: ignore
            self._is_degraded = False
            self._last_error = None
        except (OSError, RuntimeError, sqlite3.Error) as exc:
            # [SIL-2] Resilient fallback to RAM if SQLite fails
            logger.warning("MQTT spool falling back to RAM: %s", exc)
            self._is_degraded = True
            self._last_error = str(exc)
            self._cache = None
            self._deque = deque(maxlen=limit)  # type: ignore

    def close(self) -> None:
        if self._cache:
            self._cache.close()  # type: ignore
            self._cache = None

    def append(self, message: QueuedPublish) -> None:
        # [SIL-2] Use msgspec.json.encode for high-performance direct serialization
        data = msgspec.json.encode(message)

        if self.limit > 0 and len(self._deque) >= self.limit:  # type: ignore
            self._deque.popleft()  # type: ignore
            self._dropped_due_to_limit += 1

        self._deque.append(data)  # type: ignore

    def pop_next(self) -> QueuedPublish | None:
        while len(self._deque) > 0:  # type: ignore
            try:
                record_bytes = self._deque.popleft()  # type: ignore
            except (IndexError, AttributeError):
                break

            if record_bytes is None:
                break
            try:
                # Direct JSON decoding into msgspec.Struct
                return msgspec.json.decode(record_bytes, type=QueuedPublish)  # type: ignore
            except msgspec.MsgspecError as exc:
                self._corrupt_dropped += 1
                logger.warning("Dropping corrupt MQTT spool entry: %s", exc)
        return None

    def requeue(self, message: QueuedPublish) -> None:
        data = msgspec.json.encode(message)
        if self.limit > 0 and len(self._deque) >= self.limit:  # type: ignore
            self._deque.pop()  # type: ignore
            self._dropped_due_to_limit += 1
        self._deque.appendleft(data)  # type: ignore

    @property
    def pending(self) -> int:
        return len(self._deque)  # type: ignore

    @property
    def is_degraded(self) -> bool:
        """Return True if the spool is operating in RAM-only mode."""
        return self._is_degraded

    @property
    def last_error(self) -> str | None:
        """Return the last error message from the underlying queue."""
        return self._last_error

    def snapshot(self) -> dict[str, int | float]:
        return {
            "pending": self.pending,
            "limit": self.limit,
            "dropped_due_to_limit": self._dropped_due_to_limit,
            "corrupt_dropped": self._corrupt_dropped,
            "fallback_active": int(self.is_degraded),
        }


__all__ = ["QueuedPublish", "MQTTPublishSpool", "MQTTSpoolError"]
