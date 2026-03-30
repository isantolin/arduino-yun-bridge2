"""Bounded and persistent queue helpers for McuBridge runtime state."""

from __future__ import annotations

import logging
import shutil
import sqlite3
from collections import deque
from dataclasses import dataclass
from contextlib import suppress
from pathlib import Path
from threading import Lock
from typing import Any, Generic, Iterable, TypeVar, cast

from persistqueue import Empty, FIFOSQLiteQueue

T = TypeVar("T")

logger = logging.getLogger("mcubridge.state.queues")


@dataclass(frozen=True)
class QueueEvent:
    """Detailed event info for queue operations."""
    success: bool = True
    dropped_chunks: int = 0
    dropped_bytes: int = 0
    truncated_chunks: int = 0
    truncated_bytes: int = 0

    @property
    def accepted(self) -> bool:
        """Alias for success used in tests."""
        return self.success


class PersistentQueue(Generic[T]):
    """Persistent FIFO queue backed directly by persist-queue."""

    def __init__(
        self,
        directory: str | Path | None = None,
        max_items: int | None = None,
    ) -> None:
        self.directory = Path(directory) if directory else None
        self.max_items = max_items
        self._items: deque[T] = deque()
        self._store: FIFOSQLiteQueue | None = None
        self._fallback_active = False
        self._fallback_reason: str | None = None
        self._last_error: str | None = None
        self._closed = False
        self._lock = Lock()

        if self.directory is not None:
            self._open_store(load_existing=True)

    def _open_store(self, *, load_existing: bool) -> None:
        if self.directory is None or self._closed:
            return
        with self._lock:
            if self._store is not None:
                with suppress(Exception):
                    self._store.close()
                self._store = None
            try:
                self.directory.mkdir(parents=True, exist_ok=True)
                self._store = FIFOSQLiteQueue(
                    str(self.directory),
                    auto_commit=True,
                    multithreading=True,
                )
                self._fallback_active = False
                self._fallback_reason = None
                self._last_error = None
                if load_existing:
                    rows = cast(list[dict[str, Any]], self._store.queue())
                    self._items = deque(cast(T, row["data"]) for row in rows)
            except (OSError, RuntimeError, ValueError, sqlite3.Error) as exc:
                self._activate_fallback_locked("initialization_failed", exc)

    def _activate_fallback(self, reason: str, exc: BaseException | None = None) -> None:
        with self._lock:
            self._activate_fallback_locked(reason, exc)

    def _activate_fallback_locked(self, reason: str, exc: BaseException | None = None) -> None:
        if self._store is not None:
            with suppress(Exception):
                self._store.close()
            self._store = None
        self._fallback_active = True
        self._fallback_reason = reason
        self._last_error = str(exc) if exc is not None else None
        if exc is not None:
            logger.warning("Persistent queue fallback (%s): %s", reason, exc)

    def _rewrite_store(self) -> None:
        with self._lock:
            self._rewrite_store_locked()

    def _rewrite_store_locked(self) -> None:
        if self.directory is None or self._closed:
            return
        try:
            if self._store is not None:
                with suppress(Exception):
                    self._store.close()
                self._store = None

            shutil.rmtree(self.directory, ignore_errors=True)
            self.directory.mkdir(parents=True, exist_ok=True)
            self._store = FIFOSQLiteQueue(
                str(self.directory),
                auto_commit=True,
                multithreading=True,
            )
            for item in self._items:
                self._store.put(item)
        except (OSError, RuntimeError, ValueError, sqlite3.Error) as exc:
            self._activate_fallback_locked("rewrite_failed", exc)

    def __del__(self) -> None:
        # [SIL-2] Final safety check to avoid resource leaks during garbage collection.
        if not getattr(self, "_closed", True):
            try:
                if self._store is not None:
                    self._store.close()
            except Exception:
                pass
            self._store = None
            self._closed = True

    def close(self) -> None:
        with self._lock:
            self._closed = True
            if self._store is not None:
                with suppress(Exception):
                    self._store.close()
                self._store = None

    @property
    def fallback_active(self) -> bool:
        return self._fallback_active

    @property
    def fallback_reason(self) -> str | None:
        return self._fallback_reason

    @property
    def last_error(self) -> str | None:
        return self._last_error

    def append(self, item: T) -> QueueEvent:
        with self._lock:
            if self._closed:
                return QueueEvent(success=False)
            
            dropped_chunks = 0
            if self.max_items is not None and self.max_items > 0 and len(self._items) >= self.max_items:
                if self._items:
                    self._items.popleft()
                    dropped_chunks = 1

            self._items.append(item)
            if self._store is not None:
                try:
                    if dropped_chunks > 0:
                        self._store.get_nowait()
                    self._store.put(item)
                except (Empty, sqlite3.Error, RuntimeError):
                    self._activate_fallback_locked("write_failed")
            return QueueEvent(success=True, dropped_chunks=dropped_chunks)

    def appendleft(self, item: T) -> QueueEvent:
        with self._lock:
            if self._closed:
                return QueueEvent(success=False)
            self._items.appendleft(item)
            if self._store is not None:
                self._rewrite_store_locked()
            return QueueEvent(success=True)

    def popleft(self) -> T | None:
        with self._lock:
            if self._closed or not self._items:
                return None
            item = self._items.popleft()
            if self._store is not None:
                try:
                    self._store.get_nowait()
                except (Empty, sqlite3.Error, RuntimeError):
                    self._rewrite_store_locked()
            return item

    def pop(self) -> T:
        with self._lock:
            if self._closed:
                raise RuntimeError("pop from a closed queue")
            if not self._items:
                raise IndexError("pop from an empty deque")
            item = self._items.pop()
            if self._store is not None:
                self._rewrite_store_locked()
            return item

    def extend(self, items: Iterable[T]) -> None:
        for item in items:
            self.append(item)

    def clear(self) -> None:
        with self._lock:
            self._items.clear()
            if self._store is not None:
                self._rewrite_store_locked()

    def __len__(self) -> int:
        return len(self._items)

    def __getitem__(self, index: int) -> T:
        with self._lock:
            return self._items[index]

    def __iter__(self) -> Iterable[T]:
        with self._lock:
            return iter(tuple(self._items))

    def values(self) -> tuple[T, ...]:
        with self._lock:
            return tuple(self._items)


class BoundedByteDeque:
    """Byte-aware deque that maintains item and byte limits."""

    def __init__(
        self,
        max_items: int | None = None,
        max_bytes: int | None = None,
    ) -> None:
        self.max_items = max_items
        self.max_bytes = max_bytes
        self._base = PersistentQueue[bytes](max_items=max_items)
        self._bytes = 0

    def append(self, data: bytes) -> QueueEvent:
        dropped_chunks = 0
        dropped_bytes = 0
        truncated_bytes = 0
        
        if self.max_bytes is not None and self.max_bytes > 0 and len(data) > self.max_bytes:
            truncated_bytes = len(data) - self.max_bytes
            data = data[:self.max_bytes]

        while self.max_bytes is not None and self.max_bytes > 0 and self._bytes + len(data) > self.max_bytes:
            old = self.popleft()
            if old is None:
                break
            dropped_chunks += 1
            dropped_bytes += len(old)

        evt = self._base.append(data)
        if evt.success:
            self._bytes += len(data)
            return QueueEvent(
                success=True,
                dropped_chunks=dropped_chunks + evt.dropped_chunks,
                dropped_bytes=dropped_bytes,
                truncated_chunks=1 if truncated_bytes > 0 else 0,
                truncated_bytes=truncated_bytes,
            )
        return QueueEvent(success=False)

    def appendleft(self, data: bytes) -> QueueEvent:
        evt = self._base.appendleft(data)
        if evt.success:
            self._bytes += len(data)
        return evt

    def popleft(self) -> bytes:
        item = self._base.popleft()
        if item is None:
            raise IndexError("popleft from an empty deque")
        self._bytes -= len(item)
        return item

    def pop(self) -> bytes:
        item = self._base.pop()
        self._bytes -= len(item)
        return item

    def extend(self, items: Iterable[bytes]) -> None:
        for item in items:
            self.append(item)

    def clear(self) -> None:
        self._base.clear()
        self._bytes = 0

    def close(self) -> None:
        self._base.close()

    def update_limits(self, max_items: int | None = None, max_bytes: int | None = None) -> None:
        self.max_items = max_items
        self.max_bytes = max_bytes
        self._base.max_items = max_items
        
        while self.max_items is not None and self.max_items > 0 and len(self) > self.max_items:
            if self.popleft() is None: break
        while self.max_bytes is not None and self.max_bytes > 0 and self.bytes > self.max_bytes:
            if self.popleft() is None: break

    def setup_persistence(self, directory: str | Path, ram_limit: int = 100) -> None:
        del ram_limit
        previous = self._base.values()
        self._base.close()
        self._base = PersistentQueue[bytes](directory=directory, max_items=self.max_items)
        self._bytes = 0
        for item in previous:
            self.append(item)

    def __len__(self) -> int:
        return len(self._base)

    def __getitem__(self, index: int) -> bytes:
        return self._base[index]

    def __iter__(self) -> Iterable[bytes]:
        return iter(self._base)

    def __bool__(self) -> bool:
        return len(self) > 0

    @property
    def bytes(self) -> int:
        return self._bytes

    @property
    def bytes_used(self) -> int:
        return self._bytes

    @property
    def limit_bytes(self) -> int | None:
        return self.max_bytes

    @property
    def _queue(self) -> PersistentQueue[bytes]:
        return self._base


__all__ = ["PersistentQueue", "BoundedByteDeque", "QueueEvent"]
