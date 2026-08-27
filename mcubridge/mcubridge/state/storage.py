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


def _open_lmdb_env(
    path: str, db_name: bytes, default_file: str, map_size: int = 10485760
) -> tuple[lmdb.Environment | None, Any]:
    """Canonical resilient LMDB environment opener with automatic corruption recovery. [SIL-2]"""
    p = Path(path)
    if p.is_dir():
        env_path = str(p / default_file)
    else:
        p.parent.mkdir(parents=True, exist_ok=True)
        env_path = path

    for attempt in range(2):
        try:
            env = lmdb.open(
                env_path,
                max_dbs=1,
                map_size=map_size,
                readahead=False,
                meminit=False,
                map_async=True,
                subdir=False,
            )
            db = env.open_db(db_name)
            return env, db
        except (lmdb.Error, OSError) as exc:
            if attempt == 0:
                logger.warning("LMDB database corrupt or invalid, recreating", path=env_path, error=str(exc))
                target = Path(env_path)
                if target.exists():
                    try:
                        target.unlink()
                    except OSError as e:
                        logger.warning("Failed to unlink target path", path=str(target), error=str(e))
            else:
                logger.error("Failed to reinitialize LMDB environment", path=env_path, error=str(exc))
    return None, None


class LmdbDeque:
    """SIL-2 persistent FIFO queue implementation backed by LMDB C transactions."""

    def __init__(self, path: str, maxlen: int | None = None) -> None:
        self.path = path
        self.maxlen = maxlen
        self.is_mem = path.startswith(":memory:")
        self._mem: collections.deque[bytes] = collections.deque(maxlen=maxlen)
        self.env: lmdb.Environment | None = None
        self.db: Any = None

        if not self.is_mem:
            self._open_env()

    def _open_env(self) -> None:
        self.env, self.db = _open_lmdb_env(self.path, b"deque", "deque.db")

    def __len__(self) -> int:
        if self.is_mem:
            return len(self._mem)
        if not self.env:
            return 0
        with self.env.begin(db=self.db) as txn:
            return txn.stat(self.db)["entries"]

    async def append(self, item: bytes) -> None:
        if self.is_mem:
            self._mem.append(item)
            return
        if not self.env:
            return
        with self.env.begin(write=True, db=self.db) as txn:
            cur = txn.cursor(self.db)
            next_idx = (_U64.unpack(cur.key())[0] + 1) if cur.last() else 0
            txn.put(_U64.pack(next_idx), item, db=self.db)
            if self.maxlen is not None:
                while txn.stat(self.db)["entries"] > self.maxlen and cur.first():
                    cur.delete()

    async def popleft(self) -> bytes:
        if self.is_mem:
            if not self._mem:
                raise IndexError("popleft from empty deque")
            return self._mem.popleft()
        if not self.env:
            raise IndexError("popleft from empty deque")
        with self.env.begin(write=True, db=self.db, buffers=True) as txn:
            cur = txn.cursor(self.db)
            if not cur.first():
                raise IndexError("popleft from empty deque")
            val = bytes(cur.value())
            cur.delete()
            return val

    async def peek(self) -> bytes:
        if self.is_mem:
            if not self._mem:
                raise IndexError("peek from empty deque")
            return self._mem[0]
        if not self.env:
            raise IndexError("peek from empty deque")
        with self.env.begin(db=self.db, buffers=True) as txn:
            cur = txn.cursor(self.db)
            if not cur.first():
                raise IndexError("peek from empty deque")
            return bytes(cur.value())

    async def clear(self) -> None:
        if self.is_mem:
            self._mem.clear()
        elif self.env and self.db:
            with self.env.begin(write=True, db=self.db) as txn:
                txn.drop(self.db, delete=False)

    async def vacuum(self) -> None:
        """[SIL-2] Compact LMDB storage to reclaim disk space after spool flush."""
        if self.is_mem or not self.env:
            return
        p = Path(self.path)
        env_path = p / "deque.db" if p.is_dir() else p
        compact_path = Path(str(env_path) + ".compact")
        try:
            self.env.copy(str(compact_path), compact=True)
            self.env.close()
            self.env = None
            compact_path.replace(env_path)
            self._open_env()
        except (lmdb.Error, OSError) as exc:
            logger.warning("LMDB vacuum failed, compaction skipped", error=str(exc))
            try:
                compact_path.unlink(missing_ok=True)
            except OSError as unlink_err:
                logger.warning(
                    "Failed to clean up compact database file", path=str(compact_path), error=str(unlink_err)
                )

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
        self.env, self.db = _open_lmdb_env(self.path, b"cache", "cache.db")

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
            with self.env.begin(db=self.db, buffers=True) as txn:
                val = txn.get(key.encode("utf-8"))
                return bytes(val) if val is not None else default
        except (lmdb.Error, OSError) as exc:
            logger.error("LmdbCache get failed", path=self.path, key=key, error=exc)
            return default

    async def clear(self) -> None:
        if self.is_mem:
            self._mem.clear()
        elif self.env and self.db:
            with self.env.begin(write=True, db=self.db) as txn:
                txn.drop(self.db, delete=False)

    async def close(self) -> None:
        if self.env:
            self.env.close()
            self.env = None
