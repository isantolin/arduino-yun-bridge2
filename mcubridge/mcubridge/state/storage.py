"""SIL-2 Persistent Storage Primitives based on SQLite3."""

from __future__ import annotations

from pathlib import Path
import sqlite3
from typing import Callable, TypeVar

import structlog

logger = structlog.get_logger(__name__)

T = TypeVar("T")


class SqliteDeque:
    """SIL-2 persistent queue implementation over SQLite3.

    Provides append/popleft with O(1) complexity.
    Opens/closes DB on each operation to ensure thread safety and simplicity.
    """

    def __init__(self, path: str, maxlen: int | None = None) -> None:
        self.path = path
        self.maxlen = maxlen
        Path(self.path).parent.mkdir(parents=True, exist_ok=True)
        # Ensure database and table are initialized
        self._execute(lambda conn: None)

    def _recreate_db(self) -> None:
        for suffix in ("", "-wal", "-shm"):
            target_path = Path(self.path + suffix)
            if target_path.exists():
                try:
                    target_path.unlink()
                except OSError as exc:
                    logger.warning("Failed to unlink target path", path=str(target_path), error=exc)

    def _execute(self, func: Callable[[sqlite3.Connection], T]) -> T:
        try:
            conn = sqlite3.connect(self.path)
            try:
                conn.execute("PRAGMA journal_mode=WAL;")
                conn.execute("PRAGMA synchronous=NORMAL;")
                conn.execute(
                    "CREATE TABLE IF NOT EXISTS deque (id INTEGER PRIMARY KEY AUTOINCREMENT, item BLOB NOT NULL)"
                )
                with conn:
                    return func(conn)
            finally:
                conn.close()
        except (sqlite3.Error, OSError) as e:
            logger.warning("SqliteDeque database corrupt or incomplete, recreating: %s", e)
            self._recreate_db()
            conn = sqlite3.connect(self.path)
            try:
                conn.execute("PRAGMA journal_mode=WAL;")
                conn.execute("PRAGMA synchronous=NORMAL;")
                conn.execute(
                    "CREATE TABLE IF NOT EXISTS deque (id INTEGER PRIMARY KEY AUTOINCREMENT, item BLOB NOT NULL)"
                )
                with conn:
                    return func(conn)
            finally:
                conn.close()

    def append(self, item: bytes) -> None:
        def _append_impl(conn: sqlite3.Connection) -> None:
            conn.execute("INSERT INTO deque (item) VALUES (?)", (item,))
            if self.maxlen is not None:
                cursor = conn.execute("SELECT COUNT(*) FROM deque")
                count = cursor.fetchone()[0]
                if count > self.maxlen:
                    to_delete = count - self.maxlen
                    conn.execute(
                        "DELETE FROM deque WHERE id IN (SELECT id FROM deque ORDER BY id ASC LIMIT ?)", (to_delete,)
                    )

        self._execute(_append_impl)

    def popleft(self) -> bytes:
        def _popleft_impl(conn: sqlite3.Connection) -> bytes:
            cursor = conn.execute("SELECT id, item FROM deque ORDER BY id ASC LIMIT 1")
            row = cursor.fetchone()
            if row is None:
                raise IndexError("popfrom empty deque")
            row_id, item = row
            conn.execute("DELETE FROM deque WHERE id = ?", (row_id,))
            return item

        try:
            return self._execute(_popleft_impl)
        except IndexError:
            raise

    def __len__(self) -> int:
        def _len_impl(conn: sqlite3.Connection) -> int:
            cursor = conn.execute("SELECT COUNT(*) FROM deque")
            return cursor.fetchone()[0]

        return self._execute(_len_impl)

    def __getitem__(self, index: int) -> bytes:
        def _getitem_impl(conn: sqlite3.Connection) -> bytes:
            cursor = conn.execute("SELECT COUNT(*) FROM deque")
            length = cursor.fetchone()[0]

            actual_index = index
            if actual_index < 0:
                actual_index += length

            if actual_index < 0 or actual_index >= length:
                raise IndexError("deque index out of range")

            cursor = conn.execute("SELECT item FROM deque ORDER BY id ASC LIMIT 1 OFFSET ?", (actual_index,))
            row = cursor.fetchone()
            if row is None:
                raise IndexError("deque index out of range")
            return row[0]

        try:
            return self._execute(_getitem_impl)
        except IndexError:
            raise

    def clear(self) -> None:
        self._recreate_db()
        self._execute(lambda conn: None)

    def close(self) -> None:
        pass


class SqliteCache:
    """SIL-2 persistent key-value store over SQLite3."""

    def __init__(self, path: str) -> None:
        self.path = path
        Path(self.path).parent.mkdir(parents=True, exist_ok=True)
        self._execute(lambda conn: None)

    def _recreate_db(self) -> None:
        for suffix in ("", "-wal", "-shm"):
            target_path = Path(self.path + suffix)
            if target_path.exists():
                try:
                    target_path.unlink()
                except OSError as exc:
                    logger.warning("Failed to unlink target path", path=str(target_path), error=exc)

    def _execute(self, func: Callable[[sqlite3.Connection], T]) -> T:
        try:
            conn = sqlite3.connect(self.path)
            try:
                conn.execute("PRAGMA journal_mode=WAL;")
                conn.execute("PRAGMA synchronous=NORMAL;")
                conn.execute("CREATE TABLE IF NOT EXISTS cache (key TEXT PRIMARY KEY, value BLOB NOT NULL)")
                with conn:
                    return func(conn)
            finally:
                conn.close()
        except (sqlite3.Error, OSError) as e:
            logger.warning("SqliteCache database corrupt or incomplete, recreating: %s", e)
            self._recreate_db()
            conn = sqlite3.connect(self.path)
            try:
                conn.execute("PRAGMA journal_mode=WAL;")
                conn.execute("PRAGMA synchronous=NORMAL;")
                conn.execute("CREATE TABLE IF NOT EXISTS cache (key TEXT PRIMARY KEY, value BLOB NOT NULL)")
                with conn:
                    return func(conn)
            finally:
                conn.close()

    def __setitem__(self, key: str, value: bytes) -> None:
        def _setitem_impl(conn: sqlite3.Connection) -> None:
            conn.execute("INSERT OR REPLACE INTO cache (key, value) VALUES (?, ?)", (key, value))

        self._execute(_setitem_impl)

    def __getitem__(self, key: str) -> bytes:
        def _getitem_impl(conn: sqlite3.Connection) -> bytes:
            cursor = conn.execute("SELECT value FROM cache WHERE key = ?", (key,))
            row = cursor.fetchone()
            if row is None:
                raise KeyError(key)
            return row[0]

        try:
            return self._execute(_getitem_impl)
        except KeyError:
            raise

    def get(self, key: str, default: T | None = None) -> bytes | T | None:
        """Get an item with a default value. [SIL-2] Catching only expected IO errors."""
        try:

            def _get_impl(conn: sqlite3.Connection) -> bytes | T | None:
                cursor = conn.execute("SELECT value FROM cache WHERE key = ?", (key,))
                row = cursor.fetchone()
                return row[0] if row is not None else default

            return self._execute(_get_impl)
        except (sqlite3.Error, OSError) as exc:
            logger.error("SqliteCache get failed", path=self.path, key=key, error=exc)
            return default

    def clear(self) -> None:
        self._recreate_db()
        self._execute(lambda conn: None)

    def close(self) -> None:
        pass
