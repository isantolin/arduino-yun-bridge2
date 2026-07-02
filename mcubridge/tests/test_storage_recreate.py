import aiosqlite
from unittest.mock import AsyncMock, MagicMock, patch
import pytest
from typing import Any, Generator

from mcubridge.state.storage import SqliteCache, SqliteDeque


class _ExecCtx:
    """Protocol shim for aiosqlite._ExecuteContextManager.

    Implements both Awaitable and AsyncContextManager protocols, which is
    required by the production code that uses both `await conn.execute(...)`
    and `async with conn.execute(...) as cursor:` on the same connection.
    This dual-protocol cannot be replicated with AsyncMock alone in Python 3.14+.
    """

    def __init__(self, cursor: AsyncMock) -> None:
        self._cursor = cursor

    def __await__(self) -> Generator[Any, None, AsyncMock]:
        async def _coro() -> AsyncMock:
            return self._cursor

        return _coro().__await__()

    async def __aenter__(self) -> AsyncMock:
        return self._cursor

    async def __aexit__(self, *args: Any) -> None:
        pass


def _make_conn_mock(cursor_fetchone_val: object = None) -> tuple[MagicMock, AsyncMock]:
    """Return (mock_conn, mock_cursor) using AsyncMock(spec=...) — no AwaitableMockConn."""
    mock_cursor = AsyncMock(spec=aiosqlite.Cursor)
    mock_cursor.fetchone = AsyncMock(return_value=cursor_fetchone_val)
    mock_conn = MagicMock(spec=aiosqlite.Connection)
    mock_conn.execute = MagicMock(return_value=_ExecCtx(mock_cursor))
    mock_conn.commit = AsyncMock()
    return mock_conn, mock_cursor


@pytest.mark.asyncio
async def test_sqlite_deque_recreate_on_corrupt() -> None:
    mock_conn, _ = _make_conn_mock((0,))
    db_error = aiosqlite.DatabaseError("Corrupt DB")

    with (
        patch("aiosqlite.connect", new_callable=AsyncMock, side_effect=[db_error, mock_conn]) as mock_connect,
        patch("mcubridge.state.storage.Path.exists", return_value=True),
        patch("mcubridge.state.storage.Path.unlink") as mock_unlink,
        patch("mcubridge.state.storage.logger") as mock_logger,
    ):
        dq = SqliteDeque("mock_path")
        await dq.append(b"item")

        assert mock_connect.call_count == 2
        mock_connect.assert_any_call("mock_path")
        assert mock_unlink.call_count == 3
        mock_logger.warning.assert_any_call(
            "SqliteDeque database corrupt or incomplete, recreating: %s",
            db_error,
        )


@pytest.mark.asyncio
async def test_sqlite_deque_unlink_os_error() -> None:
    mock_conn, _ = _make_conn_mock((0,))
    db_error = aiosqlite.DatabaseError("Corrupt DB")

    with (
        patch("aiosqlite.connect", new_callable=AsyncMock, side_effect=[db_error, mock_conn]),
        patch("mcubridge.state.storage.Path.exists", return_value=True),
        patch("mcubridge.state.storage.Path.unlink", side_effect=OSError("Permission denied")),
        patch("mcubridge.state.storage.logger") as mock_logger,
    ):
        dq = SqliteDeque("mock_path")
        await dq.append(b"item")

        mock_logger.warning.assert_any_call(
            "Failed to unlink target path",
            path="mock_path",
            error=mock_logger.warning.call_args[1]["error"],
        )


@pytest.mark.asyncio
async def test_sqlite_cache_recreate_on_corrupt() -> None:
    mock_conn, _ = _make_conn_mock(None)
    db_error = aiosqlite.OperationalError("Corrupt DB")

    with (
        patch("aiosqlite.connect", new_callable=AsyncMock, side_effect=[db_error, mock_conn]) as mock_connect,
        patch("mcubridge.state.storage.Path.exists", return_value=True),
        patch("mcubridge.state.storage.Path.unlink") as mock_unlink,
        patch("mcubridge.state.storage.logger") as mock_logger,
    ):
        cache = SqliteCache("mock_path")
        await cache.get("test_key")

        assert mock_connect.call_count == 2
        mock_connect.assert_any_call("mock_path")
        assert mock_unlink.call_count == 3
        mock_logger.warning.assert_any_call(
            "SqliteCache database corrupt or incomplete, recreating: %s",
            db_error,
        )


@pytest.mark.asyncio
async def test_sqlite_cache_unlink_os_error() -> None:
    mock_conn, _ = _make_conn_mock(None)
    db_error = aiosqlite.OperationalError("Corrupt DB")

    with (
        patch("aiosqlite.connect", new_callable=AsyncMock, side_effect=[db_error, mock_conn]),
        patch("mcubridge.state.storage.Path.exists", return_value=True),
        patch("mcubridge.state.storage.Path.unlink", side_effect=OSError("Permission denied")),
        patch("mcubridge.state.storage.logger") as mock_logger,
    ):
        cache = SqliteCache("mock_path")
        await cache.get("test_key")

        mock_logger.warning.assert_any_call(
            "Failed to unlink target path",
            path="mock_path",
            error=mock_logger.warning.call_args[1]["error"],
        )



@pytest.mark.asyncio
async def test_sqlite_deque_recreate_on_corrupt() -> None:
    mock_conn, _ = _make_conn_mock((0,))
    db_error = aiosqlite.DatabaseError("Corrupt DB")

    with (
        patch("aiosqlite.connect", new_callable=AsyncMock, side_effect=[db_error, mock_conn]) as mock_connect,
        patch("mcubridge.state.storage.Path.exists", return_value=True),
        patch("mcubridge.state.storage.Path.unlink") as mock_unlink,
        patch("mcubridge.state.storage.logger") as mock_logger,
    ):
        dq = SqliteDeque("mock_path")
        await dq.append(b"item")

        assert mock_connect.call_count == 2
        mock_connect.assert_any_call("mock_path")
        assert mock_unlink.call_count == 3
        mock_logger.warning.assert_any_call(
            "SqliteDeque database corrupt or incomplete, recreating: %s",
            db_error,
        )


@pytest.mark.asyncio
async def test_sqlite_deque_unlink_os_error() -> None:
    mock_conn, _ = _make_conn_mock((0,))
    db_error = aiosqlite.DatabaseError("Corrupt DB")

    with (
        patch("aiosqlite.connect", new_callable=AsyncMock, side_effect=[db_error, mock_conn]),
        patch("mcubridge.state.storage.Path.exists", return_value=True),
        patch("mcubridge.state.storage.Path.unlink", side_effect=OSError("Permission denied")),
        patch("mcubridge.state.storage.logger") as mock_logger,
    ):
        dq = SqliteDeque("mock_path")
        await dq.append(b"item")

        mock_logger.warning.assert_any_call(
            "Failed to unlink target path",
            path="mock_path",
            error=mock_logger.warning.call_args[1]["error"],
        )


@pytest.mark.asyncio
async def test_sqlite_cache_recreate_on_corrupt() -> None:
    mock_conn, _ = _make_conn_mock(None)
    db_error = aiosqlite.OperationalError("Corrupt DB")

    with (
        patch("aiosqlite.connect", new_callable=AsyncMock, side_effect=[db_error, mock_conn]) as mock_connect,
        patch("mcubridge.state.storage.Path.exists", return_value=True),
        patch("mcubridge.state.storage.Path.unlink") as mock_unlink,
        patch("mcubridge.state.storage.logger") as mock_logger,
    ):
        cache = SqliteCache("mock_path")
        await cache.get("test_key")

        assert mock_connect.call_count == 2
        mock_connect.assert_any_call("mock_path")
        assert mock_unlink.call_count == 3
        mock_logger.warning.assert_any_call(
            "SqliteCache database corrupt or incomplete, recreating: %s",
            db_error,
        )


@pytest.mark.asyncio
async def test_sqlite_cache_unlink_os_error() -> None:
    mock_conn, _ = _make_conn_mock(None)
    db_error = aiosqlite.OperationalError("Corrupt DB")

    with (
        patch("aiosqlite.connect", new_callable=AsyncMock, side_effect=[db_error, mock_conn]),
        patch("mcubridge.state.storage.Path.exists", return_value=True),
        patch("mcubridge.state.storage.Path.unlink", side_effect=OSError("Permission denied")),
        patch("mcubridge.state.storage.logger") as mock_logger,
    ):
        cache = SqliteCache("mock_path")
        await cache.get("test_key")

        mock_logger.warning.assert_any_call(
            "Failed to unlink target path",
            path="mock_path",
            error=mock_logger.warning.call_args[1]["error"],
        )


