"""Durable spool for MQTT publish messages using RAM-backed files and msgspec."""

from __future__ import annotations

import collections
import errno
import logging
import os
import threading
import time
from pathlib import Path
from typing import Deque, Callable, Protocol

import msgspec

from .messages import SpoolRecord, QueuedPublish

logger = logging.getLogger("mcubridge.mqtt.spool")


class DiskQueue(Protocol):
    def append(self, item: SpoolRecord) -> None:
        ...

    def appendleft(self, item: SpoolRecord) -> None:
        ...

    def popleft(self) -> SpoolRecord:
        ...

    def close(self) -> None:
        ...

    def clear(self) -> None:
        ...

    def __len__(self) -> int:
        ...


class FileDeque:
    """Persistent deque implementation using msgspec-serialized files."""

    def __init__(self, directory: Path) -> None:
        self.directory = directory
        self.directory.mkdir(parents=True, exist_ok=True)

    def _get_next_filename(self, prefix: str = "") -> Path:
        # High-resolution timestamp for ordering
        now_ns = time.time_ns()
        # Add random suffix or sequence if needed, but ns resolution is usually enough
        # for single threaded writer.
        filename = f"{prefix}{now_ns}.msg"
        return self.directory / filename

    def _write_file(self, path: Path, item: SpoolRecord) -> None:
        data = msgspec.msgpack.encode(item)
        tmp_path = path.with_suffix(".tmp")
        try:
            with open(tmp_path, "wb") as f:
                f.write(data)
                f.flush()
                os.fsync(f.fileno())
            tmp_path.rename(path)
        except OSError as e:
            if tmp_path.exists():
                try:
                    tmp_path.unlink()
                except OSError:
                    pass
            raise e

    def append(self, item: SpoolRecord) -> None:
        # Standard append uses timestamp
        path = self._get_next_filename()
        self._write_file(path, item)

    def appendleft(self, item: SpoolRecord) -> None:
        # Prepend uses "000_" prefix to sort before timestamps
        path = self._get_next_filename(prefix="000_")
        self._write_file(path, item)

    def popleft(self) -> SpoolRecord:
        # Naive implementation: list all, sort, pick first.
        # OK for small limits (default 256).
        try:
            files = sorted([
                f for f in self.directory.iterdir()
                if f.name.endswith(".msg")
            ])
        except OSError:
            # Directory gone?
            raise IndexError("pop from an empty deque")

        if not files:
            raise IndexError("pop from an empty deque")

        target = files[0]
        try:
            data = target.read_bytes()
            item = msgspec.msgpack.decode(data, type=SpoolRecord)
            target.unlink()
            return item
        except FileNotFoundError:
            # File processed by another thread/process? Retry.
            return self.popleft()
        except (msgspec.DecodeError, OSError) as e:
            # Corrupt file or IO error on read/unlink
            logger.warning("Removing corrupt/unreadable spool file %s: %s", target, e)
            try:
                target.unlink(missing_ok=True)
            except OSError:
                pass
            # Try next
            return self.popleft()

    def close(self) -> None:
        pass

    def clear(self) -> None:
        try:
            for f in self.directory.iterdir():
                if f.name.endswith(".msg") or f.name.endswith(".tmp"):
                    try:
                        f.unlink()
                    except OSError:
                        pass
        except OSError:
            pass

    def __len__(self) -> int:
        try:
            return len([f for f in self.directory.iterdir() if f.name.endswith(".msg")])
        except OSError:
            return 0


class MQTTSpoolError(RuntimeError):
    """Raised when the filesystem spool cannot fulfill an operation."""
    def __init__(self, reason: str, *, original: BaseException | None = None) -> None:
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
        self._lock = threading.Lock()
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
                self._disk_queue = FileDeque(self.directory)
            except OSError as exc:
                logger.warning(
                    "Failed to initialize disk spool at %s; falling back "
                    "to memory-only mode. Error: %s",
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
                    self._disk_queue.close()
                except OSError:
                    logger.warning("Error closing disk queue", exc_info=True)
            self._disk_queue = None
            self._memory_queue.clear()

    def __del__(self) -> None:
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
                except OSError as exc:
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
                        # Optimization: check directory existence first?
                        # FileDeque.popleft checks iterdir.
                        # We use len() check usually, but FileDeque.len is expensive.
                        # Just try popleft and catch IndexError.
                        try:
                            record = self._disk_queue.popleft()
                        except IndexError:
                            pass # Empty
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
                logger.warning("Dropping corrupt MQTT spool entry", exc_info=True)
                self._corrupt_dropped += 1
                continue

    def requeue(self, message: QueuedPublish) -> None:
        record: SpoolRecord = message.to_record()
        with self._lock:
            if self._use_disk and self._disk_queue is not None:
                try:
                    self._disk_queue.appendleft(record)
                except OSError as exc:
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
            self._disk_queue = None
        if self._fallback_hook is not None:
            self._fallback_hook(reason)

    def _handle_disk_error(self, exc: Exception, op: str) -> None:
        reason = "disk_full" if getattr(exc, "errno", 0) == errno.ENOSPC else "io_error"
        logger.error(
            "MQTT Spool disk error during %s: %s. Switching to memory-only mode (reason=%s).",
            op,
            exc,
            reason,
        )
        self._activate_fallback(reason)

    def _trim_locked(self) -> None:
        if self.limit <= 0:
            return

        # Simple count check
        current_size = len(self._memory_queue)
        if self._disk_queue is not None:
            try:
                current_size += len(self._disk_queue)
            except OSError:
                pass

        dropped = 0
        while current_size > self.limit:
            # Drop from head (oldest). Disk first.
            try:
                if self._disk_queue is not None:
                    try:
                        self._disk_queue.popleft()
                        dropped += 1
                        current_size -= 1
                        continue
                    except IndexError:
                        pass # Disk empty
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
            logger.warning("MQTT spool limit exceeded; dropped %d oldest", dropped)


__all__ = ["QueuedPublish", "MQTTPublishSpool", "MQTTSpoolError"]
