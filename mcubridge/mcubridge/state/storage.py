"""SIL-2 Persistent Storage Primitives based on LMDB (Lightning Memory-Mapped Database)."""

from __future__ import annotations

import collections
import struct
from pathlib import Path
from typing import Any, TypeVar

import lmdb
import structlog

logger = structlog.get_logger(__name__)

T = TypeVar("T")
_U64 = struct.Struct(">Q")


class LmdbDeque:
    """SIL-2 persistent FIFO queue implementation backed by LMDB C transactions."""

    def __init__(self, path: str, maxlen: int | None = None) -> None:
        self.path = path
        self.maxlen = maxlen
        self.is_mem = path.startswith(":memory:")
        self._mem: collections.deque[bytes] = collections.deque(maxlen=maxlen)
        self._head = 0
        self._tail = 0
        self.env: lmdb.Environment | None = None
        self.db: Any = None

        if not self.is_mem:
            self._open_env()

    def _open_env(self) -> None:
        p = Path(self.path)
        if p.is_dir():
            env_path = str(p / "deque.db")
        else:
            p.parent.mkdir(parents=True, exist_ok=True)
            env_path = self.path

        try:
            self.env = lmdb.open(env_path, max_dbs=1, map_size=10485760, readahead=False, meminit=False, subdir=False)
            self.db = self.env.open_db(b"deque")
            with self.env.begin(db=self.db) as txn:
                cur = txn.cursor()
                self._head = _U64.unpack(cur.key())[0] if cur.first() else 0
                self._tail = _U64.unpack(cur.key())[0] + 1 if cur.last() else 0
        except (lmdb.Error, OSError) as exc:
            logger.warning("LmdbDeque database corrupt or invalid, recreating: %s", exc)
            self.env = None
            target = Path(env_path)
            if target.exists():
                try:
                    target.unlink()
                except OSError as e:
                    logger.warning("Failed to unlink target path", path=str(target), error=e)
            try:
                self.env = lmdb.open(
                    env_path, max_dbs=1, map_size=10485760, readahead=False, meminit=False, subdir=False
                )
                self.db = self.env.open_db(b"deque")
                self._head = self._tail = 0
            except (lmdb.Error, OSError) as e:
                logger.error("Failed to reinitialize LMDB deque: %s", e)

    def __len__(self) -> int:
        return len(self._mem) if self.is_mem else max(0, self._tail - self._head)

    async def append(self, item: bytes) -> None:
        if self.is_mem:
            self._mem.append(item)
            return
        if not self.env:
            return
        with self.env.begin(write=True, db=self.db) as txn:
            txn.put(_U64.pack(self._tail), item)
            self._tail += 1
            if self.maxlen is not None and len(self) > self.maxlen:
                txn.delete(_U64.pack(self._head))
                self._head += 1

    async def popleft(self) -> bytes:
        if self.is_mem:
            if not self._mem:
                raise IndexError("popleft from empty deque")
            return self._mem.popleft()
        if not self.env or len(self) == 0:
            raise IndexError("popleft from empty deque")
        with self.env.begin(write=True, db=self.db) as txn:
            key = _U64.pack(self._head)
            val = txn.get(key)
            if val is None:
                raise IndexError("popleft from empty deque")
            txn.delete(key)
            self._head += 1
            return val

    async def peek(self) -> bytes:
        if self.is_mem:
            if not self._mem:
                raise IndexError("peek from empty deque")
            return self._mem[0]
        if not self.env or len(self) == 0:
            raise IndexError("peek from empty deque")
        with self.env.begin(db=self.db) as txn:
            val = txn.get(_U64.pack(self._head))
            if val is None:
                raise IndexError("peek from empty deque")
            return val

    async def length(self) -> int:
        return len(self)

    async def clear(self) -> None:
        if self.is_mem:
            self._mem.clear()
        else:
            self._open_env()

    async def vacuum(self) -> None:
        pass

    async def close(self) -> None:
        if self.env:
            self.env.close()
            self.env = None


class LmdbCache:
    """SIL-2 persistent key-value cache implementation backed by LMDB."""

    def __init__(self, path: str) -> None:
        self.path = path
        self.is_mem = path.startswith(":memory:")
        self._mem: dict[str, bytes] = {}
        self.env: lmdb.Environment | None = None
        self.db: Any = None

        if not self.is_mem:
            self._open_env()

    def _open_env(self) -> None:
        p = Path(self.path)
        if p.is_dir():
            env_path = str(p / "cache.db")
        else:
            p.parent.mkdir(parents=True, exist_ok=True)
            env_path = self.path

        try:
            self.env = lmdb.open(env_path, max_dbs=1, map_size=10485760, readahead=False, meminit=False, subdir=False)
            self.db = self.env.open_db(b"cache")
        except (lmdb.Error, OSError) as exc:
            logger.warning("Failed to initialize LmdbCache schema: %s", exc)
            self.env = None
            target = Path(env_path)
            if target.exists():
                try:
                    target.unlink()
                except OSError as e:
                    logger.warning("Failed to unlink target path", path=str(target), error=e)
            try:
                self.env = lmdb.open(
                    env_path, max_dbs=1, map_size=10485760, readahead=False, meminit=False, subdir=False
                )
                self.db = self.env.open_db(b"cache")
            except (lmdb.Error, OSError) as e:
                logger.error("Failed to reinitialize LMDB cache: %s", e)

    async def set(self, key: str, value: bytes) -> None:
        if self.is_mem:
            self._mem[key] = value
            return
        if not self.env:
            return
        with self.env.begin(write=True, db=self.db) as txn:
            txn.put(key.encode("utf-8"), value)

    async def get(self, key: str, default: T | None = None) -> bytes | T | None:
        if self.is_mem:
            return self._mem.get(key, default)
        if not self.env:
            return default
        try:
            with self.env.begin(db=self.db) as txn:
                val = txn.get(key.encode("utf-8"))
                return val if val is not None else default
        except (lmdb.Error, OSError) as exc:
            logger.error("SqliteCache get failed", path=self.path, key=key, error=exc)
            return default

    async def clear(self) -> None:
        if self.is_mem:
            self._mem.clear()
        else:
            self._open_env()

    async def close(self) -> None:
        if self.env:
            self.env.close()
            self.env = None


__all__: tuple[str, ...] = ("LmdbDeque", "LmdbCache")
