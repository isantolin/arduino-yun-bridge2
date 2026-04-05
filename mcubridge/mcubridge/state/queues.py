"""Bounded and persistent queue helpers for McuBridge runtime state."""

from __future__ import annotations

import structlog
import os
import sqlite3
from collections import deque
from pathlib import Path
from threading import Lock
from typing import Generic, Iterable, Iterator, TypeVar, cast

import msgspec
from diskcache import Deque as DiskDeque

T = TypeVar("T")

logger = structlog.get_logger("mcubridge.state.queues")


class _TrackedDiskDeque:
    """Wrapper around DiskDeque that closes thread-local sqlite3 connections.

    diskcache.Cache uses threading.local() to lazily create per-thread sqlite3
    connections.  Cache.close() only closes the *calling* thread's connection.
    When spool operations run via asyncio.to_thread the worker thread's
    connection leaks.  This wrapper closes the current thread's connection
    after every mutating operation so it can never leak.
    """

    __slots__ = ("__dict__", "_deque")

    def __init__(self, directory: str) -> None:
        self._deque = DiskDeque(directory=directory)
        # Close the sqlite3 connection that Cache.__init__ leaves open in
        # _local.con so it cannot leak if the object is later GC'd without
        # an explicit close().
        self._release_thread_con()

    def _release_thread_con(self) -> None:
        """Close and remove the current thread's diskcache sqlite3 connection."""
        _local = getattr(self._deque._cache, "_local", None)  # type: ignore[reportUnknownMemberType]
        if _local is None:
            return
        con = getattr(_local, "con", None)
        if con is not None:
            try:
                con.close()
            except Exception:
                pass
            try:
                delattr(_local, "con")
            except AttributeError:
                pass

    def close(self) -> None:
        try:
            self._deque._cache.close()  # type: ignore[reportUnknownMemberType]
        except Exception:
            pass

    def append(self, item: object) -> None:
        self._deque.append(item)  # type: ignore[reportUnknownMemberType]
        self._release_thread_con()

    def appendleft(self, item: object) -> None:
        self._deque.appendleft(item)  # type: ignore[reportUnknownMemberType]
        self._release_thread_con()

    def popleft(self) -> object:
        result: object = self._deque.popleft()  # type: ignore[reportUnknownVariableType]
        self._release_thread_con()
        return result  # type: ignore[reportUnknownVariableType]

    def pop(self) -> object:
        result: object = self._deque.pop()  # type: ignore[reportUnknownVariableType]
        self._release_thread_con()
        return result  # type: ignore[reportUnknownVariableType]

    def clear(self) -> None:
        self._deque.clear()  # type: ignore[reportUnknownMemberType]
        self._release_thread_con()

    def __iter__(self) -> Iterator[object]:
        result: list[object] = list(self._deque)
        self._release_thread_con()
        return iter(result)

    def __len__(self) -> int:
        result = len(self._deque)
        self._release_thread_con()
        return result


class QueueEvent(msgspec.Struct, frozen=True):
    """Detailed event info for queue operations."""
    success: bool = True
    dropped_chunks: int = 0
    dropped_bytes: int = 0
    truncated_chunks: int = 0
    truncated_bytes: int = 0

    @property
    def accepted(self) -> bool:
        """Alias for success used in tests."""
        return self.success


class PersistentQueue(Generic[T]):
    """Persistent FIFO queue with RAM mirror for full 'deque' API compatibility."""

    def __init__(
        self,
        directory: str | Path | None = None,
        max_items: int | None = None,
    ) -> None:
        self.directory = Path(directory) if directory else None
        self.max_items = max_items
        self._items: deque[T] = deque()
        self._store: _TrackedDiskDeque | None = None
        self._fallback_active = False
        self._fallback_reason: str | None = None
        self._last_error: str | None = None
        self._closed = False
        self._lock = Lock()

        if self.directory is not None:
            self._open_store()

    def _close_store(self) -> None:
        """Close the diskcache store and all tracked sqlite3 connections."""
        if self._store is None:
            return
        try:
            self._store.close()
        except Exception as e:
            logger.error("Error closing store: %s", e)
        self._store = None

    def _open_store(self) -> None:
        if self.directory is None or self._closed:
            return
        with self._lock:
            self._close_store()
            store: _TrackedDiskDeque | None = None
            try:
                self.directory.mkdir(parents=True, exist_ok=True)
                # [SIL-2] CVE mitigation: restrict directory to owner-only
                # to prevent pickle deserialization attacks via diskcache.
                os.chmod(self.directory, 0o700)
                store = _TrackedDiskDeque(directory=str(self.directory))
                self._store = store
                self._fallback_active = False
                self._fallback_reason = None
                self._last_error = None

                # [SIL-2] Rebuild RAM mirror from Disk on startup
                self._items = deque(
                    cast(T, item)
                    for item in list(self._store)
                )
            except (OSError, RuntimeError, ValueError, sqlite3.Error) as exc:
                # If the store was created but not yet assigned, close it
                # to release the sqlite3 connection before falling back.
                if store is not None and self._store is None:
                    try:
                        store.close()
                    except Exception:
                        pass
                self._activate_fallback_locked("initialization_failed", exc)
                # Break the traceback reference cycle so that any sqlite3
                # connections captured in Cache.__init__ frames are released
                # immediately instead of waiting for the garbage collector.
                exc.__traceback__ = None

    def _activate_fallback_locked(self, reason: str, exc: BaseException | None = None) -> None:
        self._close_store()
        self._fallback_active = True
        self._fallback_reason = reason
        self._last_error = str(exc) if exc is not None else None
        if exc is not None:
            logger.warning("Persistent queue fallback (%s): %s", reason, exc)

    def __del__(self) -> None:
        try:
            if not getattr(self, "_closed", True):
                self.close()
        except Exception:
            pass

    def close(self) -> None:
        with self._lock:
            self._closed = True
            self._close_store()

    @property
    def fallback_active(self) -> bool:
        return self._fallback_active

    @property
    def fallback_reason(self) -> str | None:
        return self._fallback_reason

    @property
    def last_error(self) -> str | None:
        return self._last_error

    def append(self, item: T) -> QueueEvent:
        with self._lock:
            if self._closed:
                return QueueEvent(success=False)

            dropped_chunks = 0
            # [SIL-2] Circular buffer: only drop if we actually have a store and limits
            if self.max_items is not None and self.max_items > 0:
                while len(self._items) >= self.max_items:
                    self._items.popleft()
                    dropped_chunks += 1
                    if self._store is not None:
                        try:
                            self._store.popleft()
                        except (IndexError, sqlite3.Error):
                            pass

            self._items.append(item)
            if self._store is not None:
                try:
                    self._store.append(item)
                except (sqlite3.Error, RuntimeError) as e:
                    self._activate_fallback_locked("write_failed", e)
            return QueueEvent(success=True, dropped_chunks=dropped_chunks)

    def appendleft(self, item: T) -> QueueEvent:
        with self._lock:
            if self._closed:
                return QueueEvent(success=False)
            self._items.appendleft(item)
            if self._store is not None:
                try:
                    self._store.appendleft(item)
                except (sqlite3.Error, RuntimeError) as e:
                    self._activate_fallback_locked("write_failed", e)
            return QueueEvent(success=True)

    def popleft(self) -> T | None:
        with self._lock:
            if self._closed or not self._items:
                return None
            item = self._items.popleft()
            if self._store is not None:
                try:
                    self._store.popleft()
                except (IndexError, sqlite3.Error, RuntimeError):
                    pass
            return item

    def pop(self) -> T:
        with self._lock:
            if self._closed:
                raise RuntimeError("pop from a closed queue")
            if not self._items:
                raise IndexError("pop from an empty deque")
            item = self._items.pop()
            if self._store is not None:
                try:
                    self._store.pop()
                except (IndexError, sqlite3.Error, RuntimeError):
                    pass
            return item

    def clear(self) -> None:
        with self._lock:
            self._items.clear()
            if self._store is not None:
                try:
                    self._store.clear()
                except (sqlite3.Error, RuntimeError) as e:
                    self._activate_fallback_locked("clear_failed", e)

    def __len__(self) -> int:
        return len(self._items)

    def __getitem__(self, index: int) -> T:
        with self._lock:
            return self._items[index]

    def __iter__(self) -> Iterable[T]:
        with self._lock:
            return iter(tuple(self._items))

    def values(self) -> tuple[T, ...]:
        with self._lock:
            return tuple(self._items)


class BoundedByteDeque:
    """Byte-aware deque that maintains item and byte limits."""

    def __init__(
        self,
        max_items: int | None = None,
        max_bytes: int | None = None,
    ) -> None:
        self.max_items = max_items
        self.max_bytes = max_bytes
        self._base = PersistentQueue[bytes](max_items=max_items)
        self._bytes = 0

    def append(self, data: bytes) -> QueueEvent:
        dropped_chunks = 0
        dropped_bytes = 0
        truncated_bytes = 0

        if self.max_bytes is not None and self.max_bytes > 0 and len(data) > self.max_bytes:
            truncated_bytes = len(data) - self.max_bytes
            data = data[:self.max_bytes]

        while self.max_bytes is not None and self.max_bytes > 0 and self._bytes + len(data) > self.max_bytes:
            try:
                old = self.popleft()
                dropped_chunks += 1
                dropped_bytes += len(old)
            except IndexError:
                break

        evt = self._base.append(data)
        if evt.success:
            self._bytes += len(data)
            return QueueEvent(
                success=True,
                dropped_chunks=dropped_chunks + evt.dropped_chunks,
                dropped_bytes=dropped_bytes,
                truncated_chunks=1 if truncated_bytes > 0 else 0,
                truncated_bytes=truncated_bytes,
            )
        return QueueEvent(success=False)

    def appendleft(self, data: bytes) -> QueueEvent:
        evt = self._base.appendleft(data)
        if evt.success:
            self._bytes += len(data)
        return evt

    def popleft(self) -> bytes:
        item = self._base.popleft()
        if item is None:
            raise IndexError("popleft from an empty deque")
        self._bytes -= len(item)
        return item

    def pop(self) -> bytes:
        item = self._base.pop()
        self._bytes -= len(item)
        return item

    def extend(self, items: Iterable[bytes]) -> None:
        for item in items:
            self.append(item)

    def clear(self) -> None:
        self._base.clear()
        self._bytes = 0

    def close(self) -> None:
        self._base.close()

    def update_limits(self, max_items: int | None = None, max_bytes: int | None = None) -> None:
        self.max_items = max_items
        self.max_bytes = max_bytes
        self._base.max_items = max_items

        while self.max_items is not None and self.max_items > 0 and len(self) > self.max_items:
            try:
                self.popleft()
            except IndexError:
                break
        while self.max_bytes is not None and self.max_bytes > 0 and self.bytes > self.max_bytes:
            try:
                self.popleft()
            except IndexError:
                break

    def setup_persistence(self, directory: str | Path, ram_limit: int = 100) -> None:
        del ram_limit
        previous = self._base.values()
        self._base.close()
        self._base = PersistentQueue[bytes](directory=directory, max_items=self.max_items)
        self._bytes = 0
        for item in previous:
            self.append(item)

    def __len__(self) -> int:
        return len(self._base)

    def __getitem__(self, index: int) -> bytes:
        return self._base[index]

    def __iter__(self) -> Iterable[bytes]:
        return iter(self._base)

    def __bool__(self) -> bool:
        return len(self) > 0

    @property
    def bytes(self) -> int:
        return self._bytes

    @property
    def bytes_used(self) -> int:
        return self._bytes

    @property
    def limit_bytes(self) -> int | None:
        return self.max_bytes

    @property
    def _queue(self) -> PersistentQueue[bytes]:
        return self._base


__all__ = ["PersistentQueue", "BoundedByteDeque", "QueueEvent"]
