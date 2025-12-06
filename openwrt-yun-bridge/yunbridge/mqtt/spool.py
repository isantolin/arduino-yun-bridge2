"""Durable spool for MQTT publish messages stored on the filesystem."""
from __future__ import annotations

import errno
import logging
import threading
import time
from pathlib import Path
from typing import Any, Optional

from persistqueue import Queue
from persistqueue.exceptions import Empty

from . import PublishableMessage

logger = logging.getLogger("yunbridge.mqtt.spool")


class MQTTSpoolError(RuntimeError):
    """Raised when the filesystem spool cannot fulfill an operation."""

    def __init__(
        self,
        reason: str,
        *,
        original: Optional[BaseException] = None,
    ) -> None:
        message = reason if original is None else f"{reason}:{original}"
        super().__init__(message)
        self.reason = reason
        self.original = original


class MQTTPublishSpool:
    """Filesystem-backed spool powered by :mod:`persistqueue`."""

    def __init__(self, directory: str, limit: int) -> None:
        self.directory = Path(directory)
        self.directory.mkdir(parents=True, exist_ok=True)
        self.limit = max(0, limit)
        self._queue_dir = self.directory / "queue"
        self._queue_dir.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()
        self._queue = Queue(str(self._queue_dir), maxsize=0)
        self._dropped_due_to_limit = 0
        self._trim_events = 0
        self._last_trim_unix = 0.0
        self._corrupt_dropped = 0
        if self.limit > 0:
            with self._lock:
                self._trim_locked()

    def close(self) -> None:
        with self._lock:
            close_fn = getattr(self._queue, "close", None)
            if callable(close_fn):
                close_fn()

    def __del__(self) -> None:  # pragma: no cover - defensive cleanup
        try:
            self.close()
        except Exception:
            logger.debug("Failed to close MQTT spool cleanly", exc_info=True)

    def append(self, message: PublishableMessage) -> None:
        record = message.to_spool_record()
        with self._lock:
            try:
                self._queue.put(record)
            except OSError as exc:
                reason = "disk_full" if exc.errno == errno.ENOSPC else "append_failed"
                raise MQTTSpoolError(reason, original=exc) from exc
            except Exception as exc:  # pragma: no cover - defensive
                raise MQTTSpoolError("append_failed", original=exc) from exc
            if self.limit > 0:
                self._trim_locked()

    def pop_next(self) -> Optional[PublishableMessage]:
        while True:
            with self._lock:
                try:
                    record: Any = self._queue.get(block=False)
                except Empty:
                    return None
            try:
                return PublishableMessage.from_spool_record(record)
            except Exception:
                logger.warning(
                    "Dropping corrupt MQTT spool entry; cannot decode",
                    exc_info=True,
                )
                self._corrupt_dropped += 1
                continue

    def requeue(self, message: PublishableMessage) -> None:
        self.append(message)

    @property
    def pending(self) -> int:
        with self._lock:
            return self._queue.qsize()

    @property
    def queue_path(self) -> Path:
        return self._queue_dir

    def snapshot(self) -> dict[str, int | float]:
        return {
            "pending": self.pending,
            "limit": self.limit,
            "dropped_due_to_limit": self._dropped_due_to_limit,
            "trim_events": self._trim_events,
            "last_trim_unix": self._last_trim_unix,
            "corrupt_dropped": self._corrupt_dropped,
        }

    def _trim_locked(self) -> None:
        if self.limit <= 0:
            return
        dropped = 0
        while self._queue.qsize() > self.limit:
            try:
                self._queue.get(block=False)
                dropped += 1
            except Empty:
                break
        if dropped:
            self._dropped_due_to_limit += dropped
            self._trim_events += 1
            self._last_trim_unix = time.time()
            logger.warning(
                "MQTT spool limit %d exceeded; dropped %d oldest entrie(s)",
                self.limit,
                dropped,
            )


__all__ = ["MQTTPublishSpool", "MQTTSpoolError"]
