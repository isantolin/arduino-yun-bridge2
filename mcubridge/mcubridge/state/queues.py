"""Functional minimalistic persistent queues powered by diskcache.Cache (SIL-2).
Manual index management to ensure resource cleanup and persistence integrity.
"""

from __future__ import annotations

import os
from collections import deque
from pathlib import Path
from typing import Any, Generic, TypeVar, cast

import msgspec
import structlog
import diskcache

T = TypeVar("T")
logger = structlog.get_logger("mcubridge.state.queues")


class QueueEvent(msgspec.Struct, frozen=True):
    """Event data for queue operations."""

    success: bool = True
    dropped_chunks: int = 0
    dropped_bytes: int = 0
    truncated_bytes: int = 0


class PersistentQueue(Generic[T]):
    """FIFO Queue using diskcache.Cache with manual index control for SIL-2 compliance."""

    def __init__(
        self, directory: str | Path | None = None, max_items: int | None = None
    ) -> None:
        self.max_items = max_items
        self._closed = False
        self._fallback_active = False
        self._last_err_msg: str | None = None
        self._cache: diskcache.Cache | None = None
        self._items: deque[T] | Any = deque()

        if directory:
            try:
                self.directory = Path(directory)
                self.directory.mkdir(parents=True, exist_ok=True)
                os.chmod(self.directory, 0o700)
                # [SIL-2] Use Cache directly to ensure close() releases SQLite immediately
                self._cache = diskcache.Cache(str(self.directory))
                if "head" not in self._cache:
                    self._cache["head"] = 0
                if "tail" not in self._cache:
                    self._cache["tail"] = 0
                self._items = None
            except (OSError, Exception) as exc:
                logger.warning("Queue falling back to RAM: %s", exc)
                self._fallback_active = True
                self._last_err_msg = str(exc)
                self._items = deque()
                self._cache = None
        else:
            self._items = deque()
            self._cache = None

    def append(self, item: T) -> QueueEvent:
        if self._closed:
            return QueueEvent(success=False)
        dropped = 0
        if self.max_items and len(self) >= self.max_items:
            self.popleft()
            dropped = 1

        if self._cache is not None:
            tail: int = cast(int, self._cache["tail"])
            self._cache[tail] = item
            self._cache["tail"] = tail + 1
        else:
            self._items.append(item)
        return QueueEvent(success=True, dropped_chunks=dropped)

    def appendleft(self, item: T) -> QueueEvent:
        if self._closed:
            return QueueEvent(success=False)
        if self._cache is not None:
            head: int = cast(int, self._cache["head"]) - 1
            self._cache[head] = item
            self._cache["head"] = head
        else:
            self._items.appendleft(item)
        return QueueEvent(success=True)

    def popleft(self) -> T | None:
        if self._closed or len(self) == 0:
            return None
        if self._cache is not None:
            head: int = cast(int, self._cache["head"])
            val: Any = self._cache.get(head)  # type: ignore[reportUnknownMemberType]
            if val is not None:
                self._cache.delete(head)  # type: ignore[reportUnknownMemberType]
            self._cache["head"] = head + 1
            return cast(T, val)
        return self._items.popleft()

    def clear(self) -> None:
        if self._closed:
            return
        if self._cache is not None:
            cast(Any, self._cache).clear()  # type: ignore[reportUnknownMemberType]
            self._cache["head"] = 0
            self._cache["tail"] = 0
        else:
            self._items.clear()

    def close(self) -> None:
        """Explicitly close the cache to release SQLite file handles immediately."""
        if not getattr(self, "_closed", False):
            self._closed = True
            if hasattr(self, "_cache") and self._cache is not None:
                try:
                    cast(Any, self._cache).close()  # type: ignore[reportUnknownMemberType]
                except (OSError, RuntimeError, AttributeError, ValueError) as exc:
                    logger.warning("Failed to close memory queue cache fallback: %s", exc)
                self._cache = None

    def __del__(self) -> None:
        if not getattr(self, "_closed", True):
            self.close()

    @property
    def fallback_active(self) -> bool:
        return self._fallback_active

    @property
    def last_error(self) -> str | None:
        return self._last_err_msg

    def __len__(self) -> int:
        if self._cache is not None:
            return cast(int, self._cache["tail"]) - cast(int, self._cache["head"])
        return len(self._items)


class BoundedByteDeque:
    """Byte-aware queue wrapper with truncation support."""

    def __init__(
        self, max_bytes: int | None = None, max_items: int | None = None
    ) -> None:
        self.max_bytes = max_bytes
        self._queue = PersistentQueue[bytes](max_items=max_items)
        self._current_bytes = 0

    def append(self, data: bytes) -> QueueEvent:
        truncated = 0
        if self.max_bytes and len(data) > self.max_bytes:
            truncated = len(data) - self.max_bytes
            data = data[: self.max_bytes]

        dropped_chunks, dropped_bytes = 0, 0
        while self.max_bytes and self._current_bytes + len(data) > self.max_bytes:
            old = self.popleft()
            if old is None:
                break
            dropped_chunks += 1
            dropped_bytes += len(old)

        evt = self._queue.append(data)
        self._current_bytes += len(data)
        return QueueEvent(
            success=True,
            truncated_bytes=truncated,
            dropped_chunks=dropped_chunks + evt.dropped_chunks,
            dropped_bytes=dropped_bytes,
        )

    def appendleft(self, data: bytes) -> QueueEvent:
        self._queue.appendleft(data)
        self._current_bytes += len(data)
        return QueueEvent(success=True)

    def popleft(self) -> bytes | None:
        val = self._queue.popleft()
        if val is not None:
            self._current_bytes = max(0, self._current_bytes - len(val))
        return val

    def clear(self) -> None:
        self._queue.clear()
        self._current_bytes = 0

    def close(self) -> None:
        self._queue.close()

    def __len__(self) -> int:
        return len(self._queue)

    @property
    def bytes(self) -> int:
        return self._current_bytes


__all__ = ["PersistentQueue", "BoundedByteDeque", "QueueEvent"]
