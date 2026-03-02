"""Durable spool for MQTT publish messages using msgspec and zict."""

from __future__ import annotations

import logging
import threading
import time
from pathlib import Path
from collections.abc import Callable, MutableMapping

import msgspec
import zict

from ..protocol.structures import QueuedPublish, SpoolRecord

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
    """Hybrid spool that automates RAM/Disk management using zict."""

    def __init__(
        self,
        directory: str,
        limit: int,
        *,
        on_fallback: Callable[[str, BaseException | None], None] | None = None,
    ) -> None:
        self.directory = Path(directory)
        self.limit = max(1, limit)
        self._lock = threading.RLock()

        # [SIL-2] Flash Protection Check
        directory_str = str(self.directory)
        is_tmp = directory_str == "/tmp" or directory_str.startswith("/tmp/")

        self._slow: MutableMapping[str, SpoolRecord]
        if not is_tmp:
            logger.warning("MQTT spool directory %s is not under /tmp; persistence disabled", self.directory)
            self._slow = {}
            self._fallback_active = True
        else:
            try:
                self.directory.mkdir(parents=True, exist_ok=True)
                # [SIL-2] Use msgpack for efficient binary storage on disk
                self._slow = zict.Func(
                    msgspec.msgpack.encode,
                    lambda b: msgspec.msgpack.decode(b, type=SpoolRecord),
                    zict.File(str(self.directory)),
                )
                self._fallback_active = False
            except Exception as exc:
                logger.warning("Failed to initialize disk spool: %s. Falling back to RAM-only.", exc)
                self._slow = {}
                self._fallback_active = True
                if on_fallback:
                    on_fallback("initialization_failed", exc)

        # Fast storage (RAM) limit: 20% of total limit or 50 messages
        ram_limit = min(50, max(1, self.limit // 5))

        # Combined Buffer: RAM (fast) + Disk (slow)
        self._buffer = zict.Buffer(fast={}, slow=self._slow, n=ram_limit)

        # Ensure total limit via LRU piece
        self._spool = zict.LRU(n=self.limit, d=self._buffer)

        self._head = 0  # For pop_next
        self._tail = 0  # For append
        self._find_indices()

        self._dropped_due_to_limit = 0
        self._trim_events = 0
        self._last_trim_unix = 0.0
        self._corrupt_dropped = 0
        self._fallback_hook = on_fallback

    def _find_indices(self) -> None:
        """Recover head/tail indices from existing keys."""
        keys = [int(k) for k in self._spool.keys() if str(k).isdigit()]
        if keys:
            self._head = min(keys)
            self._tail = max(keys) + 1
        else:
            self._head = 0
            self._tail = 0

    def close(self) -> None:
        with self._lock:
            # zict.File handles closing its internal handles
            self._spool.clear()

    def append(self, message: QueuedPublish) -> None:
        record: SpoolRecord = message.to_record()
        with self._lock:
            key = str(self._tail)

            # Check if we are about to drop the oldest due to LRU limit
            if len(self._spool) >= self.limit and str(self._head) in self._spool:
                self._dropped_due_to_limit += 1
                self._trim_events += 1
                self._last_trim_unix = time.time()
                self._head += 1

            self._spool[key] = record
            self._tail += 1

    def pop_next(self) -> QueuedPublish | None:
        with self._lock:
            while self._head < self._tail:
                key = str(self._head)
                try:
                    record = self._spool.pop(key)
                    self._head += 1
                    return QueuedPublish.from_record(record)
                except KeyError:
                    self._head += 1  # Skip gaps
                except Exception as exc:
                    logger.warning("Dropping corrupt spool entry %s: %s", key, exc)
                    self._corrupt_dropped += 1
                    self._head += 1
            return None

    def requeue(self, message: QueuedPublish) -> None:
        """Push message back to the front of the queue."""
        record: SpoolRecord = message.to_record()
        with self._lock:
            self._head -= 1
            key = str(self._head)
            self._spool[key] = record

    @property
    def pending(self) -> int:
        return len(self._spool)

    @property
    def is_degraded(self) -> bool:
        return self._fallback_active

    def snapshot(self) -> dict[str, int | float]:
        return {
            "pending": self.pending,
            "limit": self.limit,
            "dropped_due_to_limit": self._dropped_due_to_limit,
            "trim_events": self._trim_events,
            "last_trim_unix": self._last_trim_unix,
            "corrupt_dropped": self._corrupt_dropped,
            "fallback_active": 1 if self._fallback_active else 0,
        }


__all__ = ["QueuedPublish", "MQTTPublishSpool", "MQTTSpoolError"]
