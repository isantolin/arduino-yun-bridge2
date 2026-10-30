"""SIL-2 Persistent Storage Primitives based on LMDB (Lightning Memory-Mapped Database)."""

from __future__ import annotations

import collections
import struct
from pathlib import Path
from threading import Lock
from typing import Any, TypeVar

import anyio.to_thread
import lmdb
import structlog
import tenacity

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

    def _cleanup_corrupt_env(retry_state: tenacity.RetryCallState) -> None:
        exc = retry_state.outcome.exception() if retry_state.outcome else None
        logger.warning("LMDB database corrupt or invalid, recreating", path=env_path, error=str(exc))
        try:
            Path(env_path).unlink(missing_ok=True)
        except OSError as e:
            logger.warning("Failed to unlink target path", path=str(Path(env_path)), error=str(e))

    retryer = tenacity.Retrying(
        stop=tenacity.stop_after_attempt(2),
        retry=tenacity.retry_if_exception_type((lmdb.Error, OSError)),
        before_sleep=_cleanup_corrupt_env,
        reraise=False,
    )
    try:
        for attempt in retryer:
            with attempt:
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
    except tenacity.RetryError as exc:
        logger.error("Failed to reinitialize LMDB environment", path=env_path, error=str(exc))
    return None, None


def vacuum_lmdb_env(path: str, default_file: str, env: lmdb.Environment | None, reopen_cb: Any) -> None:
    """Canonical resilient compaction for LMDB environments to reclaim disk space. [SIL-2]"""
    if not env:
        return
    p = Path(path)
    env_path = p / default_file if p.is_dir() else p
    compact_path = Path(str(env_path) + ".compact")
    try:
        env.copy(str(compact_path), compact=True)
        env.close()
        compact_path.replace(env_path)
        reopen_cb()
    except (lmdb.Error, OSError) as exc:
        logger.warning("LMDB vacuum failed, compaction skipped", error=str(exc))
        try:
            compact_path.unlink(missing_ok=True)
        except OSError as unlink_err:
            logger.warning("Failed to clean up compact database file", path=str(compact_path), error=str(unlink_err))


class LmdbDeque:
    """SIL-2 persistent FIFO queue implementation backed by LMDB C transactions."""

    def __init__(self, path: str, maxlen: int | None = None) -> None:
        self.path = path
        self.maxlen = maxlen
        self.is_mem = path.startswith(":memory:")
        self._mem: collections.deque[bytes] = collections.deque(maxlen=maxlen)
        self.env: lmdb.Environment | None = None
        self.db: Any = None
        self._length = 0
        self._mutation_lock = Lock()

        if not self.is_mem:
            self._open_env()

    def _open_env(self) -> None:
        self.env, self.db = _open_lmdb_env(self.path, b"deque", "deque.db")
        self._length = 0
        if self.env is not None:
            with self.env.begin(db=self.db) as txn:
                self._length = txn.stat(self.db)["entries"]

    def __len__(self) -> int:
        # Queue-depth metrics must not open a synchronous LMDB transaction.
        if self.is_mem:
            return len(self._mem)
        return self._length if self.env is not None else 0

    async def append(self, item: bytes) -> int:
        if self.is_mem:
            trimmed = 1 if (self.maxlen is not None and len(self._mem) >= self.maxlen) else 0
            self._mem.append(item)
            return trimmed
        if self.env is None:
            return 0
        return await anyio.to_thread.run_sync(self._append_sync, item)

    def _append_sync(self, item: bytes) -> int:
        with self._mutation_lock:
            if self.env is None:
                return 0
            with self.env.begin(write=True, db=self.db) as txn:
                cur = txn.cursor(self.db)
                next_idx = (_U64.unpack(cur.key())[0] + 1) if cur.last() else 0
                txn.put(_U64.pack(next_idx), item, db=self.db)
                trimmed = 0
                if self.maxlen is not None:
                    total_entries = txn.stat(self.db)["entries"]
                    if total_entries > self.maxlen and cur.first():
                        trimmed = total_entries - self.maxlen
                        for _ in range(trimmed):
                            cur.delete()
                new_length = txn.stat(self.db)["entries"]
            self._length = new_length
        return trimmed

    async def popleft(self) -> bytes:
        if self.is_mem:
            if not self._mem:
                raise IndexError("popleft from empty deque")
            return self._mem.popleft()
        return await anyio.to_thread.run_sync(self._popleft_sync)

    def _popleft_sync(self) -> bytes:
        with self._mutation_lock:
            if self.env is None:
                raise IndexError("popleft from empty deque")
            with self.env.begin(write=True, db=self.db, buffers=True) as txn:
                cur = txn.cursor(self.db)
                if not cur.first():
                    raise IndexError("popleft from empty deque")
                val = cur.pop(cur.key())
                if val is None:
                    raise IndexError("popleft from empty deque")
                result = bytes(val)
                new_length = txn.stat(self.db)["entries"]
            self._length = new_length
        return result

    async def peek(self) -> bytes:
        if self.is_mem:
            if not self._mem:
                raise IndexError("peek from empty deque")
            return self._mem[0]
        return await anyio.to_thread.run_sync(self._peek_sync)

    def _peek_sync(self) -> bytes:
        with self._mutation_lock:
            if self.env is None:
                raise IndexError("peek from empty deque")
            with self.env.begin(db=self.db, buffers=True) as txn:
                cur = txn.cursor(self.db)
                if not cur.first():
                    raise IndexError("peek from empty deque")
                return bytes(cur.value())

    async def clear(self) -> None:
        if self.is_mem:
            self._mem.clear()
        elif self.env is not None and self.db is not None:
            await anyio.to_thread.run_sync(self._clear_sync)

    def _clear_sync(self) -> None:
        with self._mutation_lock:
            if self.env is not None and self.db is not None:
                with self.env.begin(write=True, db=self.db) as txn:
                    txn.drop(self.db, delete=False)
                self._length = 0

    async def vacuum(self) -> None:
        """[SIL-2] Compact LMDB storage to reclaim disk space after spool flush."""
        if not self.is_mem and self.env:
            await anyio.to_thread.run_sync(self._vacuum_sync)

    def _vacuum_sync(self) -> None:
        with self._mutation_lock:
            if self.env is not None:
                vacuum_lmdb_env(self.path, "deque.db", self.env, self._open_env)

    async def close(self) -> None:
        if self.env is not None:
            await anyio.to_thread.run_sync(self.close_sync)

    def close_sync(self) -> None:
        """Close the LMDB environment synchronously for non-async lifecycle hooks."""
        with self._mutation_lock:
            if self.env is not None:
                self.env.close()
                self.env = None
                self._length = 0


class LmdbCache:
    """SIL-2 persistent key-value cache implementation backed by LMDB."""

    def __init__(self, path: str) -> None:
        self.path = path
        self.is_mem = path.startswith(":memory:")
        self._mem: dict[str, bytes] = {}
        self.env: lmdb.Environment | None = None
        self.db: Any = None
        self._mutation_lock = Lock()

        if not self.is_mem:
            self._open_env()

    def _open_env(self) -> None:
        self.env, self.db = _open_lmdb_env(self.path, b"cache", "cache.db")

    async def set(self, key: str, value: bytes) -> None:
        if self.is_mem:
            self._mem[key] = value
            return
        if self.env is not None:
            await anyio.to_thread.run_sync(self._set_sync, key, value)

    def _set_sync(self, key: str, value: bytes) -> None:
        with self._mutation_lock:
            if self.env is not None:
                with self.env.begin(write=True, db=self.db) as txn:
                    txn.put(key.encode("utf-8"), value, db=self.db)

    async def get(self, key: str, default: T | None = None) -> bytes | T | None:
        if self.is_mem:
            return self._mem.get(key, default)
        if self.env is None:
            return default
        try:
            return await anyio.to_thread.run_sync(self._get_sync, key, default)
        except (lmdb.Error, OSError) as exc:
            logger.error("LmdbCache get failed", path=self.path, key=key, error=exc)
            return default

    def _get_sync(self, key: str, default: T | None = None) -> bytes | T | None:
        with self._mutation_lock:
            if self.env is None:
                return default
            with self.env.begin(db=self.db, buffers=True) as txn:
                val = txn.get(key.encode("utf-8"), db=self.db)
                return bytes(val) if val is not None else default

    def __len__(self) -> int:
        if self.is_mem:
            return len(self._mem)
        with self._mutation_lock:
            if not self.env:
                return 0
            try:
                with self.env.begin(db=self.db) as txn:
                    return txn.stat(self.db)["entries"]
            except (lmdb.Error, OSError) as exc:
                logger.error("LmdbCache len failed", path=self.path, error=exc)
                return 0

    def __contains__(self, key: str) -> bool:
        if self.is_mem:
            return key in self._mem
        with self._mutation_lock:
            if not self.env:
                return False
            try:
                with self.env.begin(db=self.db, buffers=True) as txn:
                    return txn.get(key.encode("utf-8"), db=self.db) is not None
            except (lmdb.Error, OSError) as exc:
                logger.error("LmdbCache __contains__ failed", path=self.path, key=key, error=exc)
                return False

    async def close(self) -> None:
        if self.env is not None:
            await anyio.to_thread.run_sync(self.close_sync)

    def close_sync(self) -> None:
        """Close the LMDB environment synchronously for non-async lifecycle hooks."""
        with self._mutation_lock:
            if self.env is not None:
                self.env.close()
                self.env = None
