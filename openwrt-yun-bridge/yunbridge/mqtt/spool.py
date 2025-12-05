"""Durable spool for MQTT publish messages stored on the filesystem."""
from __future__ import annotations

import logging
import threading
from pathlib import Path
from typing import Any, Optional

from persistqueue import Queue
from persistqueue.exceptions import Empty

from . import PublishableMessage

logger = logging.getLogger("yunbridge.mqtt.spool")


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
        if self.limit > 0:
            with self._lock:
                self._trim_locked()

    def close(self) -> None:
        # The underlying persistqueue implementation does not expose open
        # handles, but keep this hook for API compatibility.
        return None

    def __del__(self) -> None:  # pragma: no cover - defensive cleanup
        try:
            self.close()
        except Exception:
            logger.debug("Failed to close MQTT spool cleanly", exc_info=True)

    def append(self, message: PublishableMessage) -> None:
        record = message.to_spool_record()
        with self._lock:
            self._queue.put(record)
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

    def snapshot(self) -> dict[str, int]:
        return {"pending": self.pending, "limit": self.limit}

    def _trim_locked(self) -> None:
        if self.limit <= 0:
            return
        while self._queue.qsize() > self.limit:
            try:
                self._queue.get(block=False)
            except Empty:
                break


__all__ = ["MQTTPublishSpool"]
