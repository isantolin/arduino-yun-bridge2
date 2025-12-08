"""Durable spool for MQTT publish messages with memory fallback."""
from __future__ import annotations

import collections
import errno
import logging
import threading
import time
from pathlib import Path
from typing import Deque as TypingDeque, Protocol, cast

from diskcache import Deque as DiskDeque

from .messages import QueuedPublish, SpoolRecord

logger = logging.getLogger("yunbridge.mqtt.spool")


class DiskQueue(Protocol):
    def append(self, item: SpoolRecord) -> None: ...

    def appendleft(self, item: SpoolRecord) -> None: ...

    def popleft(self) -> SpoolRecord: ...

    def close(self) -> None: ...

    def clear(self) -> None: ...

    def __len__(self) -> int: ...


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
    """Hybrid spool that degrades to memory if disk I/O fails."""

    def __init__(self, directory: str, limit: int) -> None:
        self.directory = Path(directory)
        self.limit = max(0, limit)
        self._lock = threading.Lock()
        self._memory_queue: TypingDeque[SpoolRecord] = collections.deque()
        self._disk_queue: DiskQueue | None = None
        self._use_disk = True
        self._dropped_due_to_limit = 0
        self._trim_events = 0
        self._last_trim_unix = 0.0
        self._corrupt_dropped = 0
        self._fallback_active = False

        if self._use_disk:
            try:
                self.directory.mkdir(parents=True, exist_ok=True)
                queue_dir = self.directory / "queue"
                queue_dir.mkdir(parents=True, exist_ok=True)
                self._disk_queue = cast(
                    DiskQueue, DiskDeque(directory=str(queue_dir))
                )
            except Exception as exc:
                logger.warning(
                    "Failed to initialize disk spool at %s; falling back "
                    "to memory-only mode. Error: %s",
                    directory,
                    exc,
                )
                self._activate_fallback()

        if self.limit > 0:
            with self._lock:
                self._trim_locked()

    def close(self) -> None:
        with self._lock:
            if self._disk_queue is not None:
                try:
                    close_fn = getattr(self._disk_queue, "close", None)
                    if callable(close_fn):
                        close_fn()
                    else:
                        self._disk_queue.clear()
                except Exception:
                    logger.warning("Error closing disk queue", exc_info=True)
            self._disk_queue = None
            self._memory_queue.clear()

    def __del__(self) -> None:  # pragma: no cover - defensive cleanup
        try:
            self.close()
        except Exception:
            pass

    def append(self, message: QueuedPublish) -> None:
        record: SpoolRecord = message.to_record()
        with self._lock:
            if self._use_disk and self._disk_queue is not None:
                try:
                    self._disk_queue.append(record)
                except Exception as exc:
                    self._handle_disk_error(exc, "append")
                    # Fallback immediately for this message
                    self._memory_queue.append(record)
            else:
                self._memory_queue.append(record)

            if self.limit > 0:
                self._trim_locked()

    def pop_next(self) -> QueuedPublish | None:
        while True:
            record: SpoolRecord | None = None
            with self._lock:
                # Prefer disk if active; drain memory otherwise. If fallback is
                # engaged we may still have disk entries pending, so attempt
                # them first and degrade on failure.

                if self._use_disk and self._disk_queue is not None:
                    try:
                        if len(self._disk_queue) > 0:
                            record = self._disk_queue.popleft()
                    except Exception as exc:
                        self._handle_disk_error(exc, "pop")
                        # Disk failed, try memory next loop iteration
                        continue

                # If we didn't get from disk (empty or failed), try memory
                if record is None and self._memory_queue:
                    record = self._memory_queue.popleft()

            if record is None:
                return None

            try:
                return QueuedPublish.from_record(record)
            except Exception:
                logger.warning(
                    "Dropping corrupt MQTT spool entry; cannot decode",
                    exc_info=True,
                )
                self._corrupt_dropped += 1
                continue

    def requeue(self, message: QueuedPublish) -> None:
        # Requeue by returning the record to the front so the next dequeue
        # attempt retries it immediately.
        record: SpoolRecord = message.to_record()
        with self._lock:
            if self._use_disk and self._disk_queue is not None:
                try:
                    self._disk_queue.appendleft(record)
                except Exception as exc:
                    self._handle_disk_error(exc, "requeue")
                    self._memory_queue.appendleft(record)
            else:
                self._memory_queue.appendleft(record)

    @property
    def pending(self) -> int:
        with self._lock:
            count = len(self._memory_queue)
            if self._disk_queue is not None:
                try:
                    count += len(self._disk_queue)
                except Exception:
                    pass
            return count

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

    def _activate_fallback(self) -> None:
        self._use_disk = False
        self._fallback_active = True
        if self._disk_queue is not None:
            try:
                # Attempting to salvage data from disk could be dangerous if
                # the queue is corrupt, so close it to stop further disk I/O.
                self._disk_queue.close()
            except Exception:
                pass
            self._disk_queue = None

    def _handle_disk_error(self, exc: Exception, op: str) -> None:
        reason = (
            "disk_full"
            if getattr(exc, "errno", 0) == errno.ENOSPC
            else "io_error"
        )
        message = (
            "MQTT Spool disk error during %s: %s. "
            "Switching to memory-only mode (reason=%s)."
        )
        logger.error(message, op, exc, reason)
        self._activate_fallback()
        # We don't raise MQTTSpoolError anymore; we handle it by degrading.

    def _trim_locked(self) -> None:
        if self.limit <= 0:
            return

        current_size = len(self._memory_queue)
        if self._disk_queue is not None:
            try:
                current_size += len(self._disk_queue)
            except Exception:
                # Can't count disk, assume 0 for disk part or just trim memory
                pass

        dropped = 0
        while current_size > self.limit:
            # Drop from head (oldest). Disk first if available.
            try:
                if self._disk_queue is not None and len(self._disk_queue) > 0:
                    self._disk_queue.popleft()
                    dropped += 1
                    current_size -= 1
                    continue
            except Exception:
                # Disk failure during trim, degrade
                self._activate_fallback()

            if self._memory_queue:
                self._memory_queue.popleft()
                dropped += 1
                current_size -= 1
            else:
                break  # Should not happen if size calculation correct

        if dropped:
            self._dropped_due_to_limit += dropped
            self._trim_events += 1
            self._last_trim_unix = time.time()
            logger.warning(
                "MQTT spool limit %d exceeded; dropped %d oldest entrie(s)",
                self.limit,
                dropped,
            )


__all__ = ["QueuedPublish", "MQTTPublishSpool", "MQTTSpoolError"]
