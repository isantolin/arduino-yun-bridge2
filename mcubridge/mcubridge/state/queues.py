"""Functional minimalistic persistent queues powered by diskcache.Deque (SIL-2).
Leverages library features to eliminate manual index management.
"""

from __future__ import annotations

import sqlite3
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
        self._deque: diskcache.Deque | deque[T]

        # [SIL-2] Integrated observability counters
        self.dropped_chunks: int = 0
        self.dropped_bytes: int = 0
        self.truncated_chunks: int = 0
        self.truncated_bytes: int = 0

        if directory:
            try:
                self.directory = Path(directory)
                self.directory.mkdir(parents=True, exist_ok=True)
                self._cache = diskcache.Cache(str(self.directory))
                self._deque = diskcache.Deque.fromcache(self._cache)  # type: ignore[reportUnknownMemberType]
                # Re-calculate current bytes from existing items
                for item in self._deque:  # type: ignore[reportUnknownVariableType]
                    if isinstance(item, (bytes, bytearray)):
                        self._current_bytes += len(item)
            except (OSError, RuntimeError, AttributeError, sqlite3.Error) as exc:
                # [SIL-2] Resilient fallback to RAM if SQLite fails
                logger.warning("Queue falling back to RAM: %s", exc)
                self._fallback_active = True
                self._last_err_msg = str(exc)
                self._cache = None
                self._deque = deque()
        else:
            self._deque = deque()

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
            too_many_bytes = self.max_bytes and data_len > 0 and (self._current_bytes + data_len > self.max_bytes)

            if not (too_many_items or too_many_bytes):
                break

            old = self.popleft()
            if old is None:
                break
            dropped_chunks += 1
            if isinstance(old, (bytes, bytearray)):
                dropped_bytes += len(old)

        self._deque.append(item)  # type: ignore[reportUnknownMemberType]
        self._current_bytes += data_len

        # [SIL-2] Native metrics aggregation
        self.dropped_chunks += dropped_chunks
        self.dropped_bytes += dropped_bytes
        self.truncated_bytes += truncated
        if truncated > 0:
            self.truncated_chunks += 1

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
        self._deque.appendleft(item)  # type: ignore[reportUnknownMemberType]
        self._current_bytes += data_len
        return QueueEvent(success=True)

    def popleft(self) -> T | None:
        if self._closed or len(self) == 0:
            return None

        try:
            val = self._deque.popleft()  # type: ignore[reportUnknownMemberType]
            if val is not None and isinstance(val, (bytes, bytearray)):
                self._current_bytes = max(0, self._current_bytes - len(val))
            return cast(Any, val)
        except (IndexError, AttributeError):
            return None

    def clear(self) -> None:
        if self._closed:
            return
        self._deque.clear()  # type: ignore[reportUnknownMemberType]
        self._current_bytes = 0

    def close(self) -> None:
        if not self._closed:
            self._closed = True
            if self._cache is not None:
                try:
                    self._cache.close()  # type: ignore[reportUnknownMemberType]
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
        return len(self._deque)


__all__ = ["BridgeQueue", "QueueEvent"]
