import sqlite3
from unittest.mock import MagicMock, patch

from mcubridge.state.storage import SqliteCache, SqliteDeque


def test_sqlite_deque_recreate_on_corrupt() -> None:
    mock_conn = MagicMock()
    # First call to connect raises sqlite3.DatabaseError (corruption), second succeeds
    side_effects = [sqlite3.DatabaseError("Corrupt DB"), mock_conn]

    with (
        patch("sqlite3.connect", side_effect=side_effects) as mock_connect,
        patch("mcubridge.state.storage.Path.exists", return_value=True),
        patch("mcubridge.state.storage.Path.unlink") as mock_unlink,
        patch("mcubridge.state.storage.logger") as mock_logger,
    ):
        SqliteDeque("mock_path")

        assert mock_connect.call_count == 2
        mock_connect.assert_any_call("mock_path")

        assert mock_unlink.call_count == 3
        mock_logger.warning.assert_any_call(
            "SqliteDeque database corrupt or incomplete, recreating: %s",
            side_effects[0],
        )


def test_sqlite_deque_unlink_os_error() -> None:
    mock_conn = MagicMock()
    side_effects = [sqlite3.DatabaseError("Corrupt DB"), mock_conn]

    with (
        patch("sqlite3.connect", side_effect=side_effects),
        patch("mcubridge.state.storage.Path.exists", return_value=True),
        patch("mcubridge.state.storage.Path.unlink", side_effect=OSError("Permission denied")),
        patch("mcubridge.state.storage.logger") as mock_logger,
    ):
        SqliteDeque("mock_path")

        mock_logger.warning.assert_any_call(
            "Failed to unlink target path",
            path="mock_path",
            error=mock_logger.warning.call_args[1]["error"],
        )


def test_sqlite_cache_recreate_on_corrupt() -> None:
    mock_conn = MagicMock()
    side_effects = [sqlite3.OperationalError("Corrupt DB"), mock_conn]

    with (
        patch("sqlite3.connect", side_effect=side_effects) as mock_connect,
        patch("mcubridge.state.storage.Path.exists", return_value=True),
        patch("mcubridge.state.storage.Path.unlink") as mock_unlink,
        patch("mcubridge.state.storage.logger") as mock_logger,
    ):
        SqliteCache("mock_path")

        assert mock_connect.call_count == 2
        mock_connect.assert_any_call("mock_path")

        assert mock_unlink.call_count == 3
        mock_logger.warning.assert_any_call(
            "SqliteCache database corrupt or incomplete, recreating: %s",
            side_effects[0],
        )


def test_sqlite_cache_unlink_os_error() -> None:
    mock_conn = MagicMock()
    side_effects = [sqlite3.OperationalError("Corrupt DB"), mock_conn]

    with (
        patch("sqlite3.connect", side_effect=side_effects),
        patch("mcubridge.state.storage.Path.exists", return_value=True),
        patch("mcubridge.state.storage.Path.unlink", side_effect=OSError("Permission denied")),
        patch("mcubridge.state.storage.logger") as mock_logger,
    ):
        SqliteCache("mock_path")

        mock_logger.warning.assert_any_call(
            "Failed to unlink target path",
            path="mock_path",
            error=mock_logger.warning.call_args[1]["error"],
        )
