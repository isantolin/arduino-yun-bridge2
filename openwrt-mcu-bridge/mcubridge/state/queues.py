"""Bounded queue helpers for McuBridge runtime state."""

from __future__ import annotations

from collections import deque
from collections.abc import Iterable, Iterator
from typing import Annotated

import msgspec
from mcubridge.protocol.structures import QueueEvent

def _make_deque() -> deque[bytes]:
    """Factory for msgspec default_factory to avoid lambdas."""
    return deque()


class BoundedByteDeque(msgspec.Struct):
    """Deque that enforces both item-count and byte-length limits."""

    max_items: Annotated[int | None, msgspec.Meta(ge=0)] = None
    max_bytes: Annotated[int | None, msgspec.Meta(ge=0)] = None
    _queue: deque[bytes] = msgspec.field(default_factory=_make_deque)
    _bytes: int = 0

    def __len__(self) -> int:
        return len(self._queue)

    def __bool__(self) -> bool:
        return bool(self._queue)

    def __iter__(self) -> Iterator[bytes]:
        return iter(self._queue)

    def __getitem__(self, index: int) -> bytes:
        return self._queue[index]

    @property
    def bytes_used(self) -> int:
        return self._bytes

    @property
    def limit_bytes(self) -> int | None:
        return self.max_bytes

    def clear(self) -> None:
        self._queue.clear()
        self._bytes = 0

    def update_limits(
        self,
        *,
        max_items: int | None = None,
        max_bytes: int | None = None,
    ) -> None:
        """Update limits using strict declarative validation."""
        if max_items is not None:
            self.max_items = msgspec.convert(max_items, Annotated[int, msgspec.Meta(ge=0)])
        if max_bytes is not None:
            self.max_bytes = msgspec.convert(max_bytes, Annotated[int, msgspec.Meta(ge=0)])
        self._make_room_for(0, 0)

    def append(self, chunk: bytes) -> QueueEvent:
        return self._push(chunk, left=False)

    def appendleft(self, chunk: bytes) -> QueueEvent:
        return self._push(chunk, left=True)

    def popleft(self) -> bytes:
        blob = self._queue.popleft()
        self._bytes -= len(blob)
        return blob

    def pop(self) -> bytes:
        blob = self._queue.pop()
        self._bytes -= len(blob)
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
            self._queue.appendleft(data)
        else:
            self._queue.append(data)
        self._bytes += len(data)
        event.accepted = True
        return event

    def _make_room_for(self, incoming_bytes: int, incoming_count: int) -> tuple[int, int]:
        dropped_chunks = 0
        dropped_bytes = 0

        while self._queue and not self._can_fit(incoming_bytes, incoming_count):
            removed = self._queue.popleft()
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
