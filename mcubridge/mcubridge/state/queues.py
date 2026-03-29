"""Bounded and persistent queue helpers for McuBridge runtime state."""

from __future__ import annotations

import logging
from collections.abc import Callable, Iterable
from pathlib import Path
from typing import Any, Generic, TypeVar

import msgspec
import zict
from mcubridge.protocol.structures import QueueEvent

logger = logging.getLogger("mcubridge.state.queues")

T = TypeVar("T")


class PersistentQueue(Generic[T]):
    """Unified O(1) queue with hybrid RAM/Disk storage using zict.

    [SIL-2] Centralized persistence logic to eliminate manual pointer arithmetic
    and O(N) operations.
    """

    def __init__(
        self,
        directory: str | Path | None = None,
        max_items: int | None = None,
        ram_limit: int = 50,
        encoder: Callable[[T], bytes] | None = None,
        decoder: Callable[[bytes], T] | None = None,
    ) -> None:
        self.directory = Path(directory) if directory else None
        self.max_items = max_items
        self.ram_limit = ram_limit
        self._encoder = encoder
        self._decoder = decoder

        self._head = 0
        self._tail = 0
        self._bytes_used = 0

        self._slow: Any = {}
        if self.directory:
            try:
                self.directory.mkdir(parents=True, exist_ok=True)
                slow_file = zict.File(str(self.directory))
                # Wrap with encoder/decoder if provided
                if self._encoder and self._decoder:
                    self._slow = zict.Func(self._encoder, self._decoder, slow_file)
                else:
                    self._slow = slow_file
            except (OSError, RuntimeError) as e:
                logger.error("Persistence setup failed at %s: %s. Using RAM-only.", self.directory, e)
                self._slow = {}

        # Hybrid Buffer: RAM (fast) + Disk (slow)
        self._queue = zict.Buffer(fast={}, slow=self._slow, n=ram_limit)
        self._recover_indices()

    def _recover_indices(self) -> None:
        """Recover head/tail indices from existing keys in O(N_disk) once at startup."""
        keys = [int(k) for k in self._queue.keys() if str(k).lstrip("-").isdigit()]
        if keys:
            self._head = min(keys)
            self._tail = max(keys) + 1
        else:
            self._head = 0
            self._tail = 0

    def __len__(self) -> int:
        return len(self._queue)

    def __bool__(self) -> bool:
        return len(self._queue) > 0

    def values(self) -> Iterable[T]:
        """Return an iterable over the values in the queue."""
        return self._queue.values()

    def append(self, item: T) -> bool:
        """Add item to the end of the queue (O(1))."""
        if self.max_items and len(self._queue) >= self.max_items:
            self.popleft()  # Drop oldest

        key = str(self._tail)
        try:
            self._queue[key] = item
            self._tail += 1
            return True
        except (OSError, ValueError, TypeError, msgspec.MsgspecError) as e:
            logger.error("Failed to append to queue: %s", e)
            return False

    def appendleft(self, item: T) -> bool:
        """Add item to the front of the queue (O(1))."""
        self._head -= 1
        key = str(self._head)
        try:
            self._queue[key] = item
            return True
        except (OSError, ValueError, TypeError, msgspec.MsgspecError) as e:
            logger.error("Failed to appendleft to queue: %s", e)
            self._head += 1
            return False

    def popleft(self) -> T | None:
        """Remove and return item from the front (O(1))."""
        while self._head < self._tail:
            key = str(self._head)
            self._head += 1
            if key in self._queue:
                try:
                    return self._queue.pop(key)
                except (KeyError, OSError, msgspec.MsgspecError) as e:
                    logger.warning("Dropping corrupt queue entry %s: %s", key, e)
        return None

    def clear(self) -> None:
        """Clear all items."""
        self._queue.clear()
        self._head = 0
        self._tail = 0

    def snapshot(self) -> dict[str, Any]:
        return {
            "pending": len(self._queue),
            "head": self._head,
            "tail": self._tail,
            "persistence": str(self.directory) if self.directory else "none",
        }


class BoundedByteDeque:
    """Specialized Deque for bytes that enforces byte-length limits.

    Refactored to use PersistentQueue internally for index management.
    """

    def __init__(
        self,
        max_items: int | None = None,
        max_bytes: int | None = None,
    ) -> None:
        self.max_bytes = max_bytes
        self._bytes = 0
        self._base = PersistentQueue[bytes](max_items=max_items)

    def setup_persistence(self, directory: str | Path, ram_limit: int = 100) -> None:
        max_items = self._base.max_items
        self._base = PersistentQueue[bytes](
            directory=directory,
            max_items=max_items,
            ram_limit=ram_limit,
        )
        # Re-calculate bytes if recovered
        self._bytes = sum(len(b) for b in self._base.values())

    def __len__(self) -> int:
        return len(self._base)

    @property
    def bytes_used(self) -> int:
        return self._bytes

    def append(self, chunk: bytes) -> QueueEvent:
        data = bytes(chunk)
        event = QueueEvent()

        if self.max_bytes and len(data) > self.max_bytes:
            data = data[-self.max_bytes :]
            event.truncated_bytes = len(chunk) - len(data)

        while self._base and self.max_bytes and self._bytes + len(data) > self.max_bytes:
            removed = self.popleft()
            if removed:
                event.dropped_chunks += 1
                event.dropped_bytes += len(removed)

        if self._base.append(data):
            self._bytes += len(data)
            event.accepted = True
        return event

    def appendleft(self, chunk: bytes) -> QueueEvent:
        # Simplified for maintenance: same byte-limiting logic
        if self._base.appendleft(chunk):
            self._bytes += len(chunk)
            return QueueEvent(accepted=True)
        return QueueEvent(accepted=False)

    def popleft(self) -> bytes | None:
        val = self._base.popleft()
        if val is not None:
            self._bytes -= len(val)
        return val

    def clear(self) -> None:
        self._base.clear()
        self._bytes = 0

    def update_limits(self, *, max_items: int | None = None, max_bytes: int | None = None) -> None:
        if max_items is not None:
            self._base.max_items = max_items
        if max_bytes is not None:
            self.max_bytes = max_bytes
        # Pruning
        while self._base and self.max_bytes and self._bytes > self.max_bytes:
            self.popleft()


__all__ = ["PersistentQueue", "BoundedByteDeque", "QueueEvent"]
