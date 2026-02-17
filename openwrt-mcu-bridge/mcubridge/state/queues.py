"""Bounded queue helpers for McuBridge runtime state."""

from __future__ import annotations

from collections import deque
from collections.abc import Iterable, Iterator
from typing import Annotated

import msgspec
from mcubridge.protocol.structures import QueueEvent

_UNSET = object()


def _normalize_limit(value: object) -> int | None:
    if value is None:
        return None
    if isinstance(value, int):
        return max(0, value)
    if isinstance(value, str):
        try:
            return max(0, int(value))
        except ValueError:
            pass
    return None  # Default fallback logic handled by caller if needed, or None


def _deque_factory() -> deque[bytes]:
    return deque()


class BoundedByteDeque(msgspec.Struct):
    """Deque that enforces both item-count and byte-length limits."""

    max_items: Annotated[int | None, msgspec.Meta(ge=0)] = None
    max_bytes: Annotated[int | None, msgspec.Meta(ge=0)] = None
    _queue: deque[bytes] = msgspec.field(default_factory=_deque_factory)
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
        """Update limits using declarative validation."""
        if max_items is not None:
            self.max_items = msgspec.convert(max_items, Annotated[int, msgspec.Meta(ge=0)])
        if max_bytes is not None:
            self.max_bytes = msgspec.convert(max_bytes, Annotated[int, msgspec.Meta(ge=0)])
        self._make_room_for(0, 0)

    def append(self, chunk: bytes) -> QueueEvent:
        return self._push(chunk, False)

    def appendleft(self, chunk: bytes) -> QueueEvent:
        return self._push(chunk, True)

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

    def _push(self, chunk: bytes, left: bool) -> QueueEvent:
        data = bytes(chunk)
        event = QueueEvent()

        if self.max_bytes and len(data) > self.max_bytes:
            data = data[-self.max_bytes :]
            event.truncated_bytes = len(chunk) - len(data)

        dropped_chunks, dropped_bytes = self._make_room_for(len(data), 1)
        event.dropped_chunks += dropped_chunks
        event.dropped_bytes += dropped_bytes

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
        limit_items = self.max_items
        limit_bytes = self.max_bytes

        while limit_items is not None and len(self._queue) + incoming_count > limit_items and self._queue:
            removed = self._queue.popleft()
            self._bytes -= len(removed)
            dropped_chunks += 1
            dropped_bytes += len(removed)

        if limit_bytes is not None and incoming_bytes > limit_bytes:
            return dropped_chunks, dropped_bytes

        while limit_bytes is not None and self._bytes + incoming_bytes > limit_bytes and self._queue:
            removed = self._queue.popleft()
            self._bytes -= len(removed)
            dropped_chunks += 1
            dropped_bytes += len(removed)

        return dropped_chunks, dropped_bytes

    def _can_fit(self, incoming_bytes: int, incoming_count: int) -> bool:
        limit_items = self.max_items
        limit_bytes = self.max_bytes
        if limit_bytes is not None and incoming_bytes > limit_bytes:
            return False
        if limit_items is not None and incoming_count > limit_items:
            return False
        if limit_items is not None and len(self._queue) + incoming_count > limit_items:
            return False
        if limit_bytes is not None and self._bytes + incoming_bytes > limit_bytes:
            return False
        return True


__all__ = ["BoundedByteDeque", "QueueEvent"]
