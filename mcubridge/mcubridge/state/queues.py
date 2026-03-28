"""Bounded queue helpers for McuBridge runtime state."""

from __future__ import annotations

import logging
from collections.abc import Iterable, Iterator, MutableMapping
from pathlib import Path

import zict
from mcubridge.protocol.structures import QueueEvent

logger = logging.getLogger("mcubridge.state.queues")


class BoundedByteDeque:
    """Deque that enforces both item-count and byte-length limits using zict.

    [SIL-2] Delegated LRU and persistence logic to minimize manual pointer arithmetic.
    """

    def __init__(
        self,
        max_items: int | None = None,
        max_bytes: int | None = None,
    ) -> None:
        self.max_items = max_items
        self.max_bytes = max_bytes
        self._bytes = 0
        self._head = 0
        self._tail = 0
        self._queue: MutableMapping[str, bytes] = {}

    def setup_persistence(self, directory: str | Path, ram_limit: int = 100) -> None:
        """Enable hybrid RAM/Disk storage delegating to zict.Buffer."""
        try:
            path = Path(directory)
            path.mkdir(parents=True, exist_ok=True)
            # [SIL-2] Native library delegation for file-backed storage
            slow = zict.File(str(path))
            self._queue = zict.Buffer(fast={}, slow=slow, n=ram_limit)
            logger.info("Persistence enabled for console queue (RAM limit: %d)", ram_limit)
        except (OSError, RuntimeError) as e:
            logger.error("Persistence setup failed: %s. Using RAM-only mode.", e)
            self._queue = {}

    def __len__(self) -> int:
        return len(self._queue)

    def __bool__(self) -> bool:
        return len(self._queue) > 0

    def __iter__(self) -> Iterator[bytes]:
        """Return iterator over values in FIFO order."""
        for i in range(self._head, self._tail):
            key = str(i)
            if key in self._queue:
                yield self._queue[key]

    def __getitem__(self, index: int) -> bytes:
        """Access item by index (O(index))."""
        if index < 0:
            index += len(self._queue)
        if index < 0 or index >= len(self._queue):
            raise IndexError("deque index out of range")

        count = 0
        for i in range(self._head, self._tail):
            key = str(i)
            if key in self._queue:
                if count == index:
                    return self._queue[key]
                count += 1
        raise IndexError("deque index out of range")

    @property
    def bytes_used(self) -> int:
        """Return the current total size of all items in bytes."""
        return self._bytes

    @property
    def limit_bytes(self) -> int | None:
        """Return the current byte capacity limit."""
        return self.max_bytes

    def clear(self) -> None:
        """Atomically clear all items from the queue."""
        self._queue.clear()
        self._bytes = 0
        self._head = 0
        self._tail = 0

    def update_limits(
        self,
        *,
        max_items: int | None = None,
        max_bytes: int | None = None,
    ) -> None:
        """Update limits and prune if necessary."""
        if max_items is not None:
            self.max_items = max_items
        if max_bytes is not None:
            self.max_bytes = max_bytes
        self._make_room_for(0, 0)

    def append(self, chunk: bytes) -> QueueEvent:
        """Append an item to the right side of the deque."""
        return self._push(chunk, left=False)

    def appendleft(self, chunk: bytes) -> QueueEvent:
        """Append an item to the left side of the deque."""
        return self._push(chunk, left=True)

    def extend(self, chunks: Iterable[bytes]) -> QueueEvent:
        """Append multiple items to the deque."""
        event = QueueEvent()
        for chunk in chunks:
            update = self.append(chunk)
            event.truncated_bytes += update.truncated_bytes
            event.dropped_chunks += update.dropped_chunks
            event.dropped_bytes += update.dropped_bytes
        return event

    def popleft(self) -> bytes:
        """Remove and return an item from the left side of the deque."""
        if not self._queue:
            raise IndexError("pop from an empty deque")

        # [SIL-2] Precise removal using direct library keys
        while self._head < self._tail:
            key = str(self._head)
            self._head += 1
            if key in self._queue:
                blob = self._queue.pop(key)
                self._bytes -= len(blob)
                return blob

        self.clear()
        raise IndexError("pop from an empty deque")

    def pop(self) -> bytes:
        """Remove and return an item from the right side of the deque."""
        if not self._queue:
            raise IndexError("pop from an empty deque")

        while self._tail > self._head:
            self._tail -= 1
            key = str(self._tail)
            if key in self._queue:
                blob = self._queue.pop(key)
                self._bytes -= len(blob)
                return blob

        self.clear()
        raise IndexError("pop from an empty deque")

    def _push(self, chunk: bytes, *, left: bool) -> QueueEvent:
        data = bytes(chunk)
        event = QueueEvent()

        # [SIL-2] Library-backed limit enforcement
        if self.max_bytes and len(data) > self.max_bytes:
            data = data[-self.max_bytes :]
            event.truncated_bytes = len(chunk) - len(data)

        dropped_chunks, dropped_bytes = self._make_room_for(len(data), 1)
        event.dropped_chunks = dropped_chunks
        event.dropped_bytes = dropped_bytes

        if self._can_fit(len(data), 1):
            if left:
                self._head -= 1
                key = str(self._head)
            else:
                key = str(self._tail)
                self._tail += 1

            self._queue[key] = data
            self._bytes += len(data)
            event.accepted = True
        return event

    def _make_room_for(self, incoming_bytes: int, incoming_count: int) -> tuple[int, int]:
        dropped_chunks = 0
        dropped_bytes = 0

        while self._queue and not self._can_fit(incoming_bytes, incoming_count):
            # FIFO drop: remove from head
            while self._head < self._tail:
                key = str(self._head)
                if key in self._queue:
                    removed = self._queue.pop(key)
                    self._bytes -= len(removed)
                    self._head += 1
                    dropped_chunks += 1
                    dropped_bytes += len(removed)
                    break
                self._head += 1

        return dropped_chunks, dropped_bytes

    def _can_fit(self, incoming_bytes: int, incoming_count: int) -> bool:
        if (
            self.max_items is not None
            and len(self._queue) + incoming_count > self.max_items
        ):
            return False
        if (
            self.max_bytes is not None
            and self._bytes + incoming_bytes > self.max_bytes
        ):
            return False
        return True


__all__ = ["BoundedByteDeque", "QueueEvent"]
