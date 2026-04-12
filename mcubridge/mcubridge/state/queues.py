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


class BridgeQueue(Generic[T]):
    """FIFO Queue with optional persistence and byte-aware limits (SIL-2)."""

    def __init__(
        self,
        directory: str | Path | None = None,
        max_items: int | None = None,
        max_bytes: int | None = None,
    ) -> None:
        self.max_items = max_items
        self.max_bytes = max_bytes
        self._closed = False
        self._fallback_active = False
        self._current_bytes = 0
        self._last_err_msg: str | None = None
        self._cache: diskcache.Cache | None = None
        self._items: deque[T] = deque()

        if directory:
            try:
                self.directory = Path(directory)
                self.directory.mkdir(parents=True, exist_ok=True)
                os.chmod(self.directory, 0o700)
                self._cache = diskcache.Cache(str(self.directory))
                if "head" not in self._cache:
                    self._cache["head"] = 0
                if "tail" not in self._cache:
                    self._cache["tail"] = 0
            except (OSError, RuntimeError, Exception) as exc:
                # [SIL-2] Resilient fallback to RAM if SQLite fails (e.g. I/O error in tests)
                logger.warning("Queue falling back to RAM: %s", exc)
                self._fallback_active = True
                self._last_err_msg = str(exc)
                self._cache = None
        else:
            self._cache = None

    def append(self, item: T) -> QueueEvent:
        if self._closed:
            return QueueEvent(success=False)

        truncated = 0
        data_len = 0
        if isinstance(item, (bytes, bytearray)):
            data_len = len(item)
            if self.max_bytes and data_len > self.max_bytes:
                truncated = data_len - self.max_bytes
                item = cast(T, item[: self.max_bytes])
                data_len = self.max_bytes

        dropped_chunks, dropped_bytes = 0, 0
        while True:
            too_many_items = self.max_items and len(self) >= self.max_items
            too_many_bytes = (
                self.max_bytes
                and data_len > 0
                and (self._current_bytes + data_len > self.max_bytes)
            )

            if not (too_many_items or too_many_bytes):
                break

            old = self.popleft()
            if old is None:
                break
            dropped_chunks += 1
            if isinstance(old, (bytes, bytearray)):
                dropped_bytes += len(old)

        cache = self._cache
        if cache is not None:
            tail: int = cast(int, cast(Any, cache)["tail"])
            cast(Any, cache)[tail] = item
            cast(Any, cache)["tail"] = tail + 1
        else:
            self._items.append(item)

        self._current_bytes += data_len
        return QueueEvent(
            success=True,
            dropped_chunks=dropped_chunks,
            dropped_bytes=dropped_bytes,
            truncated_bytes=truncated,
        )

    def appendleft(self, item: T) -> QueueEvent:
        if self._closed:
            return QueueEvent(success=False)

        data_len = len(item) if isinstance(item, (bytes, bytearray)) else 0

        cache = self._cache
        if cache is not None:
            head: int = cast(int, cast(Any, cache)["head"]) - 1
            cast(Any, cache)[head] = item
            cast(Any, cache)["head"] = head
        else:
            self._items.appendleft(item)

        self._current_bytes += data_len
        return QueueEvent(success=True)

    def popleft(self) -> T | None:
        if self._closed or len(self) == 0:
            return None

        val: T | None = None
        cache = self._cache
        if cache is not None:
            head: int = cast(int, cast(Any, cache)["head"])
            # [SIL-2] Use get and delete to emulate atomic pop from cache
            val = cast(Any, cache).get(head)
            if val is not None:
                cast(Any, cache).delete(head)
            cast(Any, cache)["head"] = head + 1
        else:
            val = self._items.popleft()

        if val is not None and isinstance(val, (bytes, bytearray)):
            self._current_bytes = max(0, self._current_bytes - len(val))

        return cast(Any, val)

    def clear(self) -> None:
        if self._closed:
            return
        cache = self._cache
        if cache is not None:
            cast(Any, cache).clear()
            cast(Any, cache)["head"] = 0
            cast(Any, cache)["tail"] = 0
        else:
            self._items.clear()
        self._current_bytes = 0

    def close(self) -> None:
        if not self._closed:
            self._closed = True
            cache = self._cache
            if cache is not None:
                try:
                    cast(Any, cache).close()
                except (OSError, RuntimeError, AttributeError) as exc:
                    logger.warning("Failed to close queue cache: %s", exc)
                self._cache = None

    def __del__(self) -> None:
        if not getattr(self, "_closed", True):
            self.close()

    @property
    def bytes(self) -> int:
        return self._current_bytes

    @property
    def fallback_active(self) -> bool:
        return self._fallback_active

    @property
    def last_error(self) -> str | None:
        return self._last_err_msg

    def __len__(self) -> int:
        cache = self._cache
        if cache is not None:
            return cast(int, cast(Any, cache)["tail"]) - cast(
                int, cast(Any, cache)["head"]
            )
        return len(self._items)


__all__ = ["BridgeQueue", "QueueEvent"]
