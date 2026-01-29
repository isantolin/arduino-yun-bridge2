"""Durable spool for MQTT publish messages using msgspec and file-based FIFO queue."""

from __future__ import annotations

import collections
import errno
import logging
import msgspec
import threading
import time
from pathlib import Path
from typing import Deque, Protocol, Callable, cast
from .messages import SpoolRecord, QueuedPublish

logger = logging.getLogger("mcubridge.mqtt.spool")


class DiskQueue(Protocol):
    def append(self, item: SpoolRecord) -> None: ...

    def appendleft(self, item: SpoolRecord) -> None: ...

    def popleft(self) -> SpoolRecord: ...

    def close(self) -> None: ...

    def clear(self) -> None: ...

    def __len__(self) -> int: ...


class FileSpoolDeque:
    """
    Persistent deque implementation using numbered files and msgspec.

    Provides O(1) append, appendleft, and popleft operations.
    Files are stored as JSON for transparency and easy debugging on target.
    """

    def __init__(self, directory: str) -> None:
        self._dir = Path(directory)
        self._dir.mkdir(parents=True, exist_ok=True)

        # Scan directory to find existing head/tail
        files = sorted([f.name for f in self._dir.glob("*.msg")])
        if files:
            self._head = int(files[0].split(".")[0])
            self._tail = int(files[-1].split(".")[0])
        else:
            self._head = 1000000000
            self._tail = 1000000000 - 1

    def _file_path(self, index: int) -> Path:
        return self._dir / f"{index:010d}.msg"

    def append(self, item: SpoolRecord | bytes) -> None:
        self._tail += 1
        path = self._file_path(self._tail)
        # If item is already bytes (from a test), write it directly.
        # Otherwise, encode the SpoolRecord dict.
        if isinstance(item, (bytes, bytearray)):
            path.write_bytes(item)
        else:
            path.write_bytes(msgspec.json.encode(item))

    def appendleft(self, item: SpoolRecord | bytes) -> None:
        self._head -= 1
        path = self._file_path(self._head)
        if isinstance(item, (bytes, bytearray)):
            path.write_bytes(item)
        else:
            path.write_bytes(msgspec.json.encode(item))

    def popleft(self) -> SpoolRecord:
        if len(self) == 0:
            raise IndexError("pop from an empty deque")

        path = self._file_path(self._head)
        try:
            data = path.read_bytes()
            # Decode to dict. If it fails, let the caller handle the exception.
            record = msgspec.json.decode(data)
            return cast(SpoolRecord, record)
        finally:
            path.unlink(missing_ok=True)
            self._head += 1
            # Reset counters if empty to prevent infinite drift
            if len(self) == 0:
                self._head = 1000000000
                self._tail = 1000000000 - 1

    def close(self) -> None:
        """No-op for file-based spool."""
        pass

    def clear(self) -> None:
        for f in self._dir.glob("*.msg"):
            f.unlink(missing_ok=True)
        self._head = 1000000000
        self._tail = 1000000000 - 1

    def __len__(self) -> int:
        count = self._tail - self._head + 1
        return max(0, count)


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

    def __init__(
        self,
        directory: str,
        limit: int,
        *,
        on_fallback: Callable[[str], None] | None = None,
    ) -> None:
        self.directory = Path(directory)
        self.limit = max(0, limit)
        self._lock = threading.RLock()
        self._memory_queue: Deque[SpoolRecord] = collections.deque()
        self._disk_queue: DiskQueue | None = None
        self._use_disk = True
        self._dropped_due_to_limit = 0
        self._trim_events = 0
        self._last_trim_unix = 0.0
        self._corrupt_dropped = 0
        self._fallback_active = False
        self._fallback_hook = on_fallback

        directory_str = str(self.directory)
        is_tmp = directory_str == "/tmp" or directory_str.startswith("/tmp/")
        if not is_tmp:
            logger.warning(
                "MQTT spool directory %s is not under /tmp; forcing memory-only mode",
                self.directory,
            )
            self._activate_fallback("non_tmp_directory")

        if self._use_disk:
            try:
                self.directory.mkdir(parents=True, exist_ok=True)
                self._disk_queue = cast(
                    DiskQueue,
                    FileSpoolDeque(directory=str(self.directory)),
                )
            except OSError as exc:
                logger.warning(
                    "Failed to initialize disk spool at %s; falling back " "to memory-only mode. Error: %s",
                    directory,
                    exc,
                )
                self._activate_fallback("initialization_failed")

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
                except OSError:
                    logger.warning("Error closing disk queue", exc_info=True)
            self._disk_queue = None
            self._memory_queue.clear()

    def __del__(self) -> None:  # pragma: no cover
        try:
            self.close()
        except OSError:
            pass

    def append(self, message: QueuedPublish) -> None:
        record: SpoolRecord = message.to_record()
        with self._lock:
            if self._use_disk and self._disk_queue is not None:
                try:
                    self._disk_queue.append(record)
                except (OSError, msgspec.MsgspecError) as exc:
                    self._handle_disk_error(exc, "append")
                    self._memory_queue.append(record)
            else:
                self._memory_queue.append(record)

            if self.limit > 0:
                self._trim_locked()

    def pop_next(self) -> QueuedPublish | None:
        while True:
            record: SpoolRecord | None = None
            with self._lock:
                if self._use_disk and self._disk_queue is not None:
                    try:
                        if len(self._disk_queue) > 0:
                            record = self._disk_queue.popleft()
                    except msgspec.MsgspecError as exc:
                        logger.warning(
                            "Dropping corrupt MQTT spool entry on disk; cannot decode: %s",
                            exc,
                        )
                        self._corrupt_dropped += 1
                        continue
                    except OSError as exc:
                        self._handle_disk_error(exc, "pop")
                        continue

                if record is None and self._memory_queue:
                    record = self._memory_queue.popleft()

            if record is None:
                return None

            try:
                return QueuedPublish.from_record(record)
            except (ValueError, TypeError, AttributeError):
                logger.warning(
                    "Dropping corrupt MQTT spool entry; record format invalid",
                    exc_info=True,
                )
                self._corrupt_dropped += 1
                continue

    def requeue(self, message: QueuedPublish) -> None:
        record: SpoolRecord = message.to_record()
        with self._lock:
            if self._use_disk and self._disk_queue is not None:
                try:
                    self._disk_queue.appendleft(record)
                except (OSError, msgspec.MsgspecError) as exc:
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
                except OSError:
                    logger.debug("Error counting disk queue items", exc_info=True)
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

    def _activate_fallback(self, reason: str = "fallback_activated") -> None:
        self._use_disk = False
        self._fallback_active = True
        if self._disk_queue is not None:
            try:
                self._disk_queue.close()
            except OSError:
                logger.debug("Error closing disk queue during fallback", exc_info=True)
            self._disk_queue = None
        if self._fallback_hook is not None:
            self._fallback_hook(reason)

    def _handle_disk_error(self, exc: Exception, op: str) -> None:
        reason = "disk_full" if getattr(exc, "errno", 0) == errno.ENOSPC else "io_error"
        message = "MQTT Spool disk error during %s: %s. " "Switching to memory-only mode (reason=%s)."
        logger.error(message, op, exc, reason)
        self._activate_fallback(reason)

    def _trim_locked(self) -> None:
        if self.limit <= 0:
            return

        current_size = self.pending
        dropped = 0
        while current_size > self.limit:
            try:
                if self._disk_queue is not None and len(self._disk_queue) > 0:
                    self._disk_queue.popleft()
                    dropped += 1
                    current_size -= 1
                    continue
            except OSError as exc:
                logger.error("Disk failure during trim: %s", exc)
                self._activate_fallback("trim_failed")

            if self._memory_queue:
                self._memory_queue.popleft()
                dropped += 1
                current_size -= 1
            else:
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


__all__ = ["QueuedPublish", "MQTTPublishSpool", "MQTTSpoolError"]
