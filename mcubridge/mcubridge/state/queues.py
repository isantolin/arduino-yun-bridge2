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


class BridgeQueue(Generic[T]):
    """FIFO Queue with optional persistence (SIL-2)."""

    def __init__(
        self,
        directory: str | Path | None = None,
        max_items: int | None = None,
    ) -> None:
        self.max_items = max_items
        self._closed = False
        self._fallback_active = False
        self._last_err_msg: str | None = None
        self._cache: diskcache.Cache | None = None
        self._deque: diskcache.Deque | deque[T]

        # [SIL-2] Integrated observability counters (Simplified)
        self.dropped_chunks: int = 0

        if directory:
            try:
                self.directory = Path(directory)
                self.directory.mkdir(parents=True, exist_ok=True)
                self._cache = diskcache.Cache(str(self.directory))
                self._deque = diskcache.Deque.fromcache(self._cache)  # type: ignore[reportUnknownMemberType]
            except (OSError, RuntimeError, AttributeError, sqlite3.Error) as exc:
                # [SIL-2] Resilient fallback to RAM if SQLite fails
                logger.warning("Queue falling back to RAM: %s", exc)
                self._fallback_active = True
                self._last_err_msg = str(exc)
                self._cache = None
                self._deque = deque(maxlen=max_items)
        else:
            self._deque = deque(maxlen=max_items)

    def append(self, item: T) -> QueueEvent:
        if self._closed:
            return QueueEvent(success=False)

        dropped = 0
        if self.max_items and len(self) >= self.max_items:
            dropped = 1
            if isinstance(self._deque, diskcache.Deque):
                self._deque.popleft()  # type: ignore[reportUnknownMemberType]

        self._deque.append(item)  # type: ignore[reportUnknownMemberType]
        self.dropped_chunks += dropped

        return QueueEvent(success=True, dropped_chunks=dropped)

    def appendleft(self, item: T) -> QueueEvent:
        if self._closed:
            return QueueEvent(success=False)

        self._deque.appendleft(item)  # type: ignore[reportUnknownMemberType]
        return QueueEvent(success=True)

    def popleft(self) -> T | None:
        if self._closed or len(self) == 0:
            return None

        try:
            val = self._deque.popleft()  # type: ignore[reportUnknownMemberType]
            return cast(Any, val)
        except (IndexError, AttributeError):
            return None

    def clear(self) -> None:
        if self._closed:
            return
        self._deque.clear()  # type: ignore[reportUnknownMemberType]

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
        # Re-calculation on the fly is expensive but avoids manual tracking
        # For SIL-2, we prefer native library state if available.
        # Since diskcache.Cache has volume, we use it.
        if self._cache:
            return self._cache.volume()  # type: ignore[reportUnknownMemberType]
        return 0

    @property
    def fallback_active(self) -> bool:
        return self._fallback_active

    @property
    def last_error(self) -> str | None:
        return self._last_err_msg

    def __len__(self) -> int:
        return len(self._deque)


__all__ = ["BridgeQueue", "QueueEvent"]
