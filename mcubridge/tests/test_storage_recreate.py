from unittest.mock import MagicMock, patch
import pytest
import lmdb

from mcubridge.state.storage import LmdbCache, LmdbDeque


@pytest.mark.asyncio
async def test_sqlite_deque_recreate_on_corrupt(tmp_path: object) -> None:
    db_path = str(tmp_path) + "/deque_test"
    db_error = lmdb.Error("Corrupt DB")

    mock_env = MagicMock()
    mock_env.open_db.return_value = MagicMock()
    mock_txn = MagicMock()
    mock_txn.get.return_value = None
    mock_txn.cursor.return_value = MagicMock(first=MagicMock(return_value=False), last=MagicMock(return_value=False))
    mock_env.begin.return_value.__enter__.return_value = mock_txn

    with (
        patch("lmdb.open", side_effect=[db_error, mock_env]) as mock_open,
        patch("mcubridge.state.storage.logger") as mock_logger,
    ):
        dq = LmdbDeque(db_path)
        await dq.append(b"item")

        assert mock_open.call_count == 2
        mock_logger.warning.assert_any_call(
            "LMDB database corrupt or invalid, recreating",
            path=db_path,
            error=str(db_error),
        )


@pytest.mark.asyncio
async def test_lmdb_deque_unlink_os_error(tmp_path: object) -> None:
    db_path = str(tmp_path) + "/deque_unlink_test"
    db_error = lmdb.Error("Corrupt DB")

    mock_env = MagicMock()
    mock_env.open_db.return_value = MagicMock()

    with (
        patch("lmdb.open", side_effect=[db_error, mock_env]),
        patch("mcubridge.state.storage.Path.exists", return_value=True),
        patch("mcubridge.state.storage.Path.unlink", side_effect=OSError("Permission denied")),
        patch("mcubridge.state.storage.logger") as mock_logger,
    ):
        LmdbDeque(db_path)

        unlink_call = next(
            c for c in mock_logger.warning.call_args_list if c.args and c.args[0] == "Failed to unlink target path"
        )
        assert unlink_call.kwargs["path"] == db_path


@pytest.mark.asyncio
async def test_lmdb_cache_recreate_on_corrupt(tmp_path: object) -> None:
    db_path = str(tmp_path) + "/cache_test"
    db_error = lmdb.Error("Corrupt DB")

    mock_env = MagicMock()
    mock_env.open_db.return_value = MagicMock()

    with (
        patch("lmdb.open", side_effect=[db_error, mock_env]) as mock_open,
        patch("mcubridge.state.storage.logger") as mock_logger,
    ):
        cache = LmdbCache(db_path)
        await cache.get("test_key")

        assert mock_open.call_count == 2
        mock_logger.warning.assert_any_call(
            "LMDB database corrupt or invalid, recreating",
            path=db_path,
            error=str(db_error),
        )


@pytest.mark.asyncio
async def test_lmdb_cache_unlink_os_error(tmp_path: object) -> None:
    db_path = str(tmp_path) + "/cache_unlink_test"
    db_error = lmdb.Error("Corrupt DB")

    mock_env = MagicMock()
    mock_env.open_db.return_value = MagicMock()

    with (
        patch("lmdb.open", side_effect=[db_error, mock_env]),
        patch("mcubridge.state.storage.Path.exists", return_value=True),
        patch("mcubridge.state.storage.Path.unlink", side_effect=OSError("Permission denied")),
        patch("mcubridge.state.storage.logger") as mock_logger,
    ):
        LmdbCache(db_path)

        unlink_call = next(
            c for c in mock_logger.warning.call_args_list if c.args and c.args[0] == "Failed to unlink target path"
        )
        assert unlink_call.kwargs["path"] == db_path
