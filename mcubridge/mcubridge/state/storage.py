"""SIL-2 Persistent Storage Primitives based on aiosqlite."""

from __future__ import annotations

from collections import deque
from pathlib import Path
from typing import Awaitable, Callable, TypeVar
import asyncio
import aiosqlite
import structlog

logger = structlog.get_logger(__name__)

T = TypeVar("T")


class SqliteDeque:
    """SIL-2 persistent queue implementation over aiosqlite.

    Provides append/popleft with O(1) complexity.
    """

    def __init__(self, path: str, maxlen: int | None = None) -> None:
        self.path = path
        self.maxlen = maxlen
        self._length = 0
        Path(self.path).parent.mkdir(parents=True, exist_ok=True)
        import sqlite3

        try:
            conn = sqlite3.connect(self.path)
            try:
                conn.execute("PRAGMA journal_mode=WAL;")
                conn.execute("PRAGMA synchronous=NORMAL;")
                conn.execute(
                    "CREATE TABLE IF NOT EXISTS deque (id INTEGER PRIMARY KEY AUTOINCREMENT, item BLOB NOT NULL)"
                )
                conn.commit()
                cursor = conn.execute("SELECT COUNT(*) FROM deque")
                row = cursor.fetchone()
                self._length = row[0] if row else 0
            finally:
                conn.close()
        except (sqlite3.Error, OSError):
            self._length = 0

    def __len__(self) -> int:
        return self._length

    async def _recreate_db(self) -> None:
        for suffix in ("", "-wal", "-shm"):
            target_path = Path(self.path + suffix)
            if target_path.exists():
                try:
                    target_path.unlink()
                except OSError as exc:
                    logger.warning("Failed to unlink target path", path=str(target_path), error=exc)
        self._length = 0

    @staticmethod
    async def _init_deque_db(conn: aiosqlite.Connection) -> None:
        """Apply WAL pragmas and ensure the deque schema exists."""
        await conn.execute("PRAGMA journal_mode=WAL;")
        await conn.execute("PRAGMA synchronous=NORMAL;")
        await conn.execute(
            "CREATE TABLE IF NOT EXISTS deque (id INTEGER PRIMARY KEY AUTOINCREMENT, item BLOB NOT NULL)"
        )
        await conn.commit()

    async def _execute(self, func: Callable[[aiosqlite.Connection], Awaitable[T]]) -> T:
        conn = None
        try:
            conn = await aiosqlite.connect(self.path)
            await self._init_deque_db(conn)
            res = await func(conn)
            await conn.commit()
            return res
        except (aiosqlite.Error, OSError) as e:
            logger.warning("SqliteDeque database corrupt or incomplete, recreating: %s", e)
            await self._recreate_db()
            if conn is not None:
                await asyncio.shield(conn.close())
                conn = None
            conn = await aiosqlite.connect(self.path)
            await self._init_deque_db(conn)
            res = await func(conn)
            await conn.commit()
            return res
        finally:
            if conn is not None:
                await asyncio.shield(conn.close())

    async def append(self, item: bytes) -> None:
        async def _append_impl(conn: aiosqlite.Connection) -> None:
            await conn.execute("INSERT INTO deque (item) VALUES (?)", (item,))
            self._length += 1
            if self.maxlen is not None and self._length > self.maxlen:
                to_delete = self._length - self.maxlen
                await conn.execute(
                    "DELETE FROM deque WHERE id IN (SELECT id FROM deque ORDER BY id ASC LIMIT ?)",
                    (to_delete,),
                )
                self._length = self.maxlen

        await self._execute(_append_impl)

    async def popleft(self) -> bytes:
        async def _popleft_impl(conn: aiosqlite.Connection) -> bytes:
            async with conn.execute(
                "DELETE FROM deque WHERE id = (SELECT MIN(id) FROM deque) RETURNING item"
            ) as cursor:
                row = await cursor.fetchone()
            if row is None:
                raise IndexError("popfrom empty deque")
            self._length = max(0, self._length - 1)
            return row[0]

        try:
            return await self._execute(_popleft_impl)
        except IndexError:
            raise

    async def length(self) -> int:
        return self._length

    async def peek(self) -> bytes:
        async def _peek_impl(conn: aiosqlite.Connection) -> bytes:
            async with conn.execute("SELECT item FROM deque ORDER BY id ASC LIMIT 1") as cursor:
                row = await cursor.fetchone()
            if row is None:
                raise IndexError("peek from empty deque")
            return row[0]

        try:
            return await self._execute(_peek_impl)
        except IndexError:
            raise

    async def clear(self) -> None:
        await self._recreate_db()

        async def _no_op(conn: aiosqlite.Connection) -> None:
            pass

        await self._execute(_no_op)

    async def close(self) -> None:
        pass


class SqliteCache:
    """SIL-2 persistent key-value store over aiosqlite."""

    def __init__(self, path: str) -> None:
        self.path = path
        Path(self.path).parent.mkdir(parents=True, exist_ok=True)

    async def _recreate_db(self) -> None:
        for suffix in ("", "-wal", "-shm"):
            target_path = Path(self.path + suffix)
            if target_path.exists():
                try:
                    target_path.unlink()
                except OSError as exc:
                    logger.warning("Failed to unlink target path", path=str(target_path), error=exc)

    @staticmethod
    async def _init_cache_db(conn: aiosqlite.Connection) -> None:
        """Apply WAL pragmas and ensure the cache schema exists."""
        await conn.execute("PRAGMA journal_mode=WAL;")
        await conn.execute("PRAGMA synchronous=NORMAL;")
        await conn.execute("CREATE TABLE IF NOT EXISTS cache (key TEXT PRIMARY KEY, value BLOB NOT NULL)")
        await conn.commit()

    async def _execute(self, func: Callable[[aiosqlite.Connection], Awaitable[T]]) -> T:
        conn = None
        try:
            conn = await aiosqlite.connect(self.path)
            await self._init_cache_db(conn)
            res = await func(conn)
            await conn.commit()
            return res
        except (aiosqlite.Error, OSError) as e:
            logger.warning("SqliteCache database corrupt or incomplete, recreating: %s", e)
            await self._recreate_db()
            if conn is not None:
                await asyncio.shield(conn.close())
                conn = None
            conn = await aiosqlite.connect(self.path)
            await self._init_cache_db(conn)
            res = await func(conn)
            await conn.commit()
            return res
        finally:
            if conn is not None:
                await asyncio.shield(conn.close())

    async def set(self, key: str, value: bytes) -> None:
        async def _setitem_impl(conn: aiosqlite.Connection) -> None:
            await conn.execute("INSERT OR REPLACE INTO cache (key, value) VALUES (?, ?)", (key, value))

        await self._execute(_setitem_impl)

    async def get(self, key: str, default: T | None = None) -> bytes | T | None:
        """Get an item with a default value. [SIL-2] Catching only expected IO errors."""
        try:

            async def _get_impl(conn: aiosqlite.Connection) -> bytes | T | None:
                async with conn.execute("SELECT value FROM cache WHERE key = ?", (key,)) as cursor:
                    row = await cursor.fetchone()
                return row[0] if row is not None else default

            return await self._execute(_get_impl)
        except (aiosqlite.Error, OSError) as exc:
            logger.error("SqliteCache get failed", path=self.path, key=key, error=exc)
            return default

    async def clear(self) -> None:
        await self._recreate_db()

        async def _no_op(conn: aiosqlite.Connection) -> None:
            pass

        await self._execute(_no_op)

    async def close(self) -> None:
        pass


class InMemoryDeque:
    """Async RAM-backed fallback queue for SIL-2 compatibility."""

    def __init__(self, maxlen: int | None = None) -> None:
        self._deque: deque[bytes] = deque(maxlen=maxlen)

    def __len__(self) -> int:
        return len(self._deque)

    async def append(self, item: bytes) -> None:
        self._deque.append(item)

    async def popleft(self) -> bytes:
        return self._deque.popleft()

    async def length(self) -> int:
        return len(self._deque)

    async def peek(self) -> bytes:
        if not self._deque:
            raise IndexError("peek from empty deque")
        return self._deque[0]

    async def clear(self) -> None:
        self._deque.clear()

    async def close(self) -> None:
        pass
