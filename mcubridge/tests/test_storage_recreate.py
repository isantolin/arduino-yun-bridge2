import aiosqlite
from unittest.mock import AsyncMock, MagicMock, patch
import pytest
from typing import Any, Generator

from mcubridge.state.storage import SqliteCache, SqliteDeque


class MockCursorHelper:
    def __init__(self, val: Any = None) -> None:
        self.val = val

    def __await__(self) -> Generator[Any, None, "MockCursorHelper"]:
        async def _await_impl() -> MockCursorHelper:
            return self

        return _await_impl().__await__()

    async def __aenter__(self) -> "MockCursorHelper":
        return self

    async def __aexit__(self, exc_type: Any, exc_val: Any, exc_tb: Any) -> None:
        pass

    async def fetchone(self) -> Any:
        return self.val


@pytest.mark.asyncio
async def test_sqlite_deque_recreate_on_corrupt() -> None:
    mock_conn = AsyncMock()
    entered_conn = mock_conn.__aenter__.return_value
    entered_conn.execute = MagicMock(return_value=MockCursorHelper((0,)))

    # First call to connect raises aiosqlite.DatabaseError (corruption), second succeeds
    side_effects = [aiosqlite.DatabaseError("Corrupt DB"), mock_conn]

    with (
        patch("aiosqlite.connect", side_effect=side_effects) as mock_connect,
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
            side_effects[0],
        )


@pytest.mark.asyncio
async def test_sqlite_deque_unlink_os_error() -> None:
    mock_conn = AsyncMock()
    entered_conn = mock_conn.__aenter__.return_value
    entered_conn.execute = MagicMock(return_value=MockCursorHelper((0,)))

    side_effects = [aiosqlite.DatabaseError("Corrupt DB"), mock_conn]

    with (
        patch("aiosqlite.connect", side_effect=side_effects),
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
    mock_conn = AsyncMock()
    entered_conn = mock_conn.__aenter__.return_value
    entered_conn.execute = MagicMock(return_value=MockCursorHelper(None))

    side_effects = [aiosqlite.OperationalError("Corrupt DB"), mock_conn]

    with (
        patch("aiosqlite.connect", side_effect=side_effects) as mock_connect,
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
            side_effects[0],
        )


@pytest.mark.asyncio
async def test_sqlite_cache_unlink_os_error() -> None:
    mock_conn = AsyncMock()
    entered_conn = mock_conn.__aenter__.return_value
    entered_conn.execute = MagicMock(return_value=MockCursorHelper(None))

    side_effects = [aiosqlite.OperationalError("Corrupt DB"), mock_conn]

    with (
        patch("aiosqlite.connect", side_effect=side_effects),
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
