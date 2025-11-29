"""Durable spool for MQTT publish messages stored on the filesystem."""
from __future__ import annotations

import json
import logging
import threading
from collections import deque
from pathlib import Path
from typing import Deque, Optional

from . import PublishableMessage

logger = logging.getLogger("yunbridge.mqtt.spool")

_ENTRY_SUFFIX = ".json"


class MQTTPublishSpool:
    """Filesystem-backed spool to avoid losing MQTT publications."""

    def __init__(self, directory: str, limit: int) -> None:
        self.directory = Path(directory)
        self.directory.mkdir(parents=True, exist_ok=True)
        self.limit = max(0, limit)
        self._queue_dir = self.directory / "queue"
        self._queue_dir.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()
        self._pending = 0
        self._sequence = 0
        self._queue: Deque[Path] = deque()
        self._load_existing()
        if self.limit:
            with self._lock:
                self._trim_locked()

    def close(self) -> None:
        # No persistent handles to close; kept for backwards compatibility.
        return None

    def __del__(self) -> None:  # pragma: no cover - defensive cleanup
        try:
            self.close()
        except Exception:
            logger.debug("Failed to close MQTT spool cleanly", exc_info=True)

    def append(self, message: PublishableMessage) -> None:
        record = json.dumps(message.to_spool_record(), separators=(",", ":"))
        encoded = record.encode("utf-8")
        with self._lock:
            entry_path = self._next_entry_path()
            tmp_path = entry_path.with_name(entry_path.name + ".tmp")
            tmp_path.write_bytes(encoded)
            tmp_path.replace(entry_path)
            self._queue.append(entry_path)
            self._pending += 1
            if self.limit:
                self._trim_locked()

    def pop_next(self) -> Optional[PublishableMessage]:
        with self._lock:
            while self._queue:
                path = self._queue.popleft()
                payload: Optional[str]
                try:
                    payload = path.read_text(encoding="utf-8")
                except FileNotFoundError:
                    payload = None
                finally:
                    try:
                        path.unlink(missing_ok=True)
                    except OSError:
                        logger.debug(
                            "Failed to remove MQTT spool entry %s", path.name,
                            exc_info=True,
                        )
                self._pending = max(0, self._pending - 1)
                if payload is None:
                    continue
                try:
                    record = json.loads(payload)
                    return PublishableMessage.from_spool_record(record)
                except Exception:
                    logger.warning(
                        "Dropping corrupt MQTT spool entry %s", path.name,
                        exc_info=True,
                    )
                    continue
            return None

    def requeue(self, message: PublishableMessage) -> None:
        self.append(message)

    @property
    def pending(self) -> int:
        with self._lock:
            return self._pending

    @property
    def queue_path(self) -> Path:
        return self._queue_dir

    def snapshot(self) -> dict[str, int]:
        return {"pending": self.pending, "limit": self.limit}

    def _load_existing(self) -> None:
        files = sorted(self._queue_dir.glob(f"*{_ENTRY_SUFFIX}"))
        for path in files:
            try:
                seq = self._parse_sequence(path)
            except ValueError:
                logger.warning(
                    "Ignoring unexpected MQTT spool entry %s", path.name,
                )
                continue
            self._sequence = max(self._sequence, seq + 1)
            self._queue.append(path)
        self._pending = len(self._queue)

    def _next_entry_path(self) -> Path:
        candidate = self._queue_dir / f"{self._sequence:020d}{_ENTRY_SUFFIX}"
        self._sequence += 1
        return candidate

    def _parse_sequence(self, path: Path) -> int:
        stem = path.stem
        if not stem.isdigit():
            raise ValueError(f"Invalid spool filename: {path.name}")
        return int(stem)

    def _trim_locked(self) -> None:
        if self.limit <= 0 or self._pending <= self.limit:
            return
        surplus = self._pending - self.limit
        for _ in range(surplus):
            if not self._queue:
                self._pending = 0
                break
            victim = self._queue.popleft()
            try:
                victim.unlink(missing_ok=True)
            except OSError:
                logger.debug(
                    "Failed to delete trimmed MQTT spool entry %s", victim.name,
                    exc_info=True,
                )
            self._pending = max(0, self._pending - 1)



__all__ = ["MQTTPublishSpool"]
