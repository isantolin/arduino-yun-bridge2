"""Bounded and persistent queue helpers for McuBridge runtime state."""

from __future__ import annotations

import logging
import shutil
import sqlite3
from contextlib import suppress
from collections import deque
from collections.abc import Iterable, Iterator
from pathlib import Path
from typing import Any, Generic, TypeVar, cast

from persistqueue import Empty, FIFOSQLiteQueue

from mcubridge.protocol.structures import QueueEvent

logger = logging.getLogger("mcubridge.state.queues")

T = TypeVar("T")
_DEFAULT_SENTINEL = object()


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

        if self.directory is not None:
            self._open_store(load_existing=True)

    def _open_store(self, *, load_existing: bool) -> None:
        if self.directory is None:
            return
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
            self._activate_fallback("initialization_failed", exc)

    def _activate_fallback(self, reason: str, exc: BaseException | None = None) -> None:
        self.close()
        self._store = None
        self._fallback_active = True
        self._fallback_reason = reason
        self._last_error = str(exc) if exc is not None else None
        if exc is not None:
            logger.warning("Persistent queue fallback (%s): %s", reason, exc)

    def _rewrite_store(self) -> None:
        if self.directory is None or self._store is None:
            return
        try:
            self._store.close()
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
            self._activate_fallback("rewrite_failed", exc)

    def __len__(self) -> int:
        return len(self._items)

    def __bool__(self) -> bool:
        return bool(self._items)

    def values(self) -> Iterable[T]:
        return tuple(self._items)

    @property
    def fallback_active(self) -> bool:
        return self._fallback_active

    @property
    def fallback_reason(self) -> str | None:
        return self._fallback_reason

    @property
    def last_error(self) -> str | None:
        return self._last_error

    def append(self, item: T) -> bool:
        dropped_left = False
        if self.max_items is not None and self.max_items > 0 and len(self._items) >= self.max_items:
            if self._items:
                self._items.popleft()
                dropped_left = True
                if self._store is not None:
                    try:
                        self._store.get_nowait()
                    except Empty:
                        dropped_left = False
                        self._rewrite_store()
                    except (OSError, RuntimeError, ValueError, sqlite3.Error) as exc:
                        self._activate_fallback("append_failed", exc)

        self._items.append(item)
        if self._store is not None:
            try:
                if dropped_left:
                    self._store.put(item)
                else:
                    self._store.put(item)
            except (OSError, RuntimeError, ValueError, sqlite3.Error) as exc:
                self._activate_fallback("append_failed", exc)
        return True

    def appendleft(self, item: T) -> bool:
        self._items.appendleft(item)
        if self.max_items is not None and self.max_items > 0 and len(self._items) > self.max_items:
            self._items.pop()
        if self._store is not None:
            self._rewrite_store()
        return True

    def popleft(self) -> T | None:
        if not self._items:
            return None
        item = self._items.popleft()
        if self._store is not None:
            try:
                self._store.get_nowait()
            except Empty:
                self._rewrite_store()
            except (OSError, RuntimeError, ValueError, sqlite3.Error) as exc:
                self._activate_fallback("read_failed", exc)
        return item

    def pop(self) -> T | None:
        if not self._items:
            return None
        item = self._items.pop()
        if self._store is not None:
            self._rewrite_store()
        return item

    def clear(self) -> None:
        self._items.clear()
        if self._store is not None:
            self._rewrite_store()

    def close(self) -> None:
        if self._store is not None:
            with suppress(OSError, RuntimeError, ValueError, sqlite3.Error):
                self._store.close()
            self._store = None

    def snapshot(self) -> dict[str, Any]:
        return {
            "pending": len(self._items),
            "persistence": str(self.directory) if self.directory else "memory",
            "fallback_active": 1 if self._fallback_active else 0,
            "fallback_reason": self._fallback_reason,
        }


class BoundedByteDeque:
    """Deque for bytes with item and byte limits, optionally persisted."""

    def __init__(
        self,
        max_items: int | None = None,
        max_bytes: int | None = None,
    ) -> None:
        self._max_items = max_items
        self.max_bytes = max_bytes
        self._bytes = 0
        self._base = PersistentQueue[bytes]()
        self._queue: Any = self._base.values()

    def setup_persistence(self, directory: str | Path, ram_limit: int = 100) -> None:
        del ram_limit
        previous = tuple(self._base.values())
        self._base.close()
        self._base = PersistentQueue[bytes](directory=directory)
        if self._base.fallback_active:
            self._queue = {}
            for chunk in previous:
                self._base.append(chunk)
        else:
            self._queue = self._base
            for chunk in previous:
                self._base.append(chunk)
        self._bytes = sum(len(chunk) for chunk in self._base.values())

    def __len__(self) -> int:
        return len(self._base)

    def __bool__(self) -> bool:
        return bool(self._base)

    def __iter__(self) -> Iterator[bytes]:
        return iter(self._base.values())

    def __getitem__(self, index: int) -> bytes:
        items = tuple(self._base.values())
        return items[index]

    @property
    def bytes_used(self) -> int:
        return self._bytes

    @property
    def limit_bytes(self) -> int | None:
        return self.max_bytes

    def append(self, chunk: bytes) -> QueueEvent:
        data = bytes(chunk)
        event = QueueEvent()

        if self.max_bytes is not None and self.max_bytes > 0 and len(data) > self.max_bytes:
            data = data[-self.max_bytes :]
            event.truncated_bytes = len(chunk) - len(data)

        while self._needs_room_for(data):
            removed = self.popleft(default=None)
            if removed is None:
                break
            event.dropped_chunks += 1
            event.dropped_bytes += len(removed)

        if self._base.append(data):
            self._bytes += len(data)
            event.accepted = True
        return event

    def appendleft(self, chunk: bytes) -> QueueEvent:
        data = bytes(chunk)
        event = QueueEvent()

        if self.max_bytes is not None and self.max_bytes > 0 and len(data) > self.max_bytes:
            data = data[-self.max_bytes :]
            event.truncated_bytes = len(chunk) - len(data)

        while self._needs_room_for(data):
            removed = self.pop(default=None)
            if removed is None:
                break
            event.dropped_chunks += 1
            event.dropped_bytes += len(removed)

        if self._base.appendleft(data):
            self._bytes += len(data)
            event.accepted = True
        return event

    def extend(self, chunks: Iterable[bytes]) -> None:
        for chunk in chunks:
            self.append(chunk)

    def popleft(self, default: object = _DEFAULT_SENTINEL) -> bytes | None:
        val = self._base.popleft()
        if val is None:
            if default is _DEFAULT_SENTINEL:
                raise IndexError("pop from an empty deque")
            return cast(bytes | None, default)
        self._bytes = max(0, self._bytes - len(val))
        return val

    def pop(self, default: object = _DEFAULT_SENTINEL) -> bytes | None:
        val = self._base.pop()
        if val is None:
            if default is _DEFAULT_SENTINEL:
                raise IndexError("pop from an empty deque")
            return cast(bytes | None, default)
        self._bytes = max(0, self._bytes - len(val))
        return val

    def clear(self) -> None:
        self._base.clear()
        self._bytes = 0

    def close(self) -> None:
        self._base.close()

    def update_limits(self, *, max_items: int | None = None, max_bytes: int | None = None) -> None:
        if max_items is not None:
            self._max_items = max_items
        if max_bytes is not None:
            self.max_bytes = max_bytes

        while self._needs_room_for(b""):
            removed = self.popleft(default=None)
            if removed is None:
                break

    def _needs_room_for(self, data: bytes) -> bool:
        if self._max_items is not None and self._max_items > 0 and len(self._base) >= self._max_items:
            return True
        if self.max_bytes is not None and self.max_bytes > 0 and self._bytes + len(data) > self.max_bytes:
            return True
        return False


__all__ = ["PersistentQueue", "BoundedByteDeque", "QueueEvent"]
