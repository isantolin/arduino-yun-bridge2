"""Bounded queue helpers for McuBridge runtime state."""

from __future__ import annotations

import logging
from collections.abc import Iterable, Iterator, MutableMapping
from pathlib import Path

import zict
from mcubridge.protocol.structures import QueueEvent

logger = logging.getLogger("mcubridge.state.queues")


class BoundedByteDeque:
    """Deque that enforces both item-count and byte-length limits with zict backend."""

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
        # Default to RAM-only until setup_persistence is called
        self._queue: MutableMapping[str, bytes] = {}

    def setup_persistence(self, directory: str | Path, ram_limit: int = 100) -> None:
        """Enable hybrid RAM/Disk storage for the deque."""
        try:
            path = Path(directory)
            path.mkdir(parents=True, exist_ok=True)
            slow = zict.File(str(path))
            self._queue = zict.Buffer(fast={}, slow=slow, n=ram_limit)
            logger.info("Persistence enabled for console queue at %s (RAM limit: %d)", directory, ram_limit)
        except Exception as e:
            logger.error("Failed to setup persistence for console queue: %s. Falling back to RAM.", e)
            self._queue = {}

    def __len__(self) -> int:
        return len(self._queue)

    def __bool__(self) -> bool:
        return len(self._queue) > 0

    def __iter__(self) -> Iterator[bytes]:
        # Return iterator over values in FIFO order (sorted keys)
        for k in sorted(self._queue.keys(), key=int):
            yield self._queue[k]

    def __getitem__(self, index: int) -> bytes:
        # Note: This is O(N) because we need to sort keys to find the index.
        # Mostly used for tests/debugging.
        keys = sorted(self._queue.keys(), key=int)
        return self._queue[keys[index]]

    @property
    def bytes_used(self) -> int:
        return self._bytes

    @property
    def limit_bytes(self) -> int | None:
        return self.max_bytes

    def clear(self) -> None:
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
        return self._push(chunk, left=False)

    def appendleft(self, chunk: bytes) -> QueueEvent:
        return self._push(chunk, left=True)

    def popleft(self) -> bytes:
        if not self._queue:
            raise IndexError("popfrom an empty deque")

        # In FIFO mode, the oldest item is at self._head
        keys = sorted(self._queue.keys(), key=int)
        key = keys[0]
        blob = self._queue.pop(key)
        self._bytes -= len(blob)

        # If we emptied the queue, reset indices to prevent overflow over time
        if not self._queue:
            self._head = 0
            self._tail = 0
        return blob

    def pop(self) -> bytes:
        if not self._queue:
            raise IndexError("pop from an empty deque")

        keys = sorted(self._queue.keys(), key=int)
        key = keys[-1]
        blob = self._queue.pop(key)
        self._bytes -= len(blob)

        if not self._queue:
            self._head = 0
            self._tail = 0
        return blob

    def extend(self, chunks: Iterable[bytes]) -> QueueEvent:
        event = QueueEvent()
        for chunk in chunks:
            update = self.append(chunk)
            event.truncated_bytes += update.truncated_bytes
            event.dropped_chunks += update.dropped_chunks
            event.dropped_bytes += update.dropped_bytes
        return event

    def _push(self, chunk: bytes, *, left: bool) -> QueueEvent:
        data = bytes(chunk)
        event = QueueEvent()

        # [SIL-2] Truncate incoming chunk if it's larger than the entire buffer budget
        if self.max_bytes and len(data) > self.max_bytes:
            data = data[-self.max_bytes :]
            event.truncated_bytes = len(chunk) - len(data)

        # Ensure room for the new chunk
        dropped_chunks, dropped_bytes = self._make_room_for(len(data), 1)
        event.dropped_chunks = dropped_chunks
        event.dropped_bytes = dropped_bytes

        if not self._can_fit(len(data), 1):
            return event

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
            # FIFO drop: remove the smallest key
            keys = sorted(self._queue.keys(), key=int)
            key = keys[0]
            removed = self._queue.pop(key)
            self._bytes -= len(removed)
            dropped_chunks += 1
            dropped_bytes += len(removed)

        return dropped_chunks, dropped_bytes

    def _can_fit(self, incoming_bytes: int, incoming_count: int) -> bool:
        if self.max_items is not None and len(self._queue) + incoming_count > self.max_items:
            return False
        if self.max_bytes is not None and self._bytes + incoming_bytes > self.max_bytes:
            return False
        return True


__all__ = ["BoundedByteDeque", "QueueEvent"]
