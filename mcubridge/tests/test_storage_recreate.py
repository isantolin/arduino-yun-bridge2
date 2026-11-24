"""Tests for LMDB database recreation on corruption and error handling."""

from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock

import lmdb
import pytest
from mcubridge.state.storage import LmdbCache, LmdbDeque, vacuum_lmdb_env
from pytest_mock import MockerFixture


async def test_sqlite_deque_recreate_on_corrupt(tmp_path: object, mocker: MockerFixture) -> None:
    db_path = str(tmp_path) + "/deque_test"
    db_error = lmdb.Error("Corrupt DB")

    mock_env = MagicMock()
    mock_env.open_db.return_value = MagicMock()
    mock_txn = MagicMock()
    mock_txn.get.return_value = None
    mock_txn.cursor.return_value = MagicMock(first=MagicMock(return_value=False), last=MagicMock(return_value=False))
    mock_env.begin.return_value.__enter__.return_value = mock_txn

    mock_open = mocker.patch("lmdb.open", side_effect=[db_error, mock_env])
    mock_logger = mocker.patch("mcubridge.state.storage.logger")

    dq = LmdbDeque(db_path)
    await dq.append(b"item")

    assert mock_open.call_count == 2
    mock_logger.warning.assert_any_call(
        "LMDB database corrupt or invalid, recreating",
        path=db_path,
        error=str(db_error),
    )


async def test_lmdb_deque_unlink_os_error(tmp_path: object, mocker: MockerFixture) -> None:
    db_path = str(tmp_path) + "/deque_unlink_test"
    db_error = lmdb.Error("Corrupt DB")

    mock_env = MagicMock()
    mock_env.open_db.return_value = MagicMock()

    mocker.patch("lmdb.open", side_effect=[db_error, mock_env])
    mocker.patch("mcubridge.state.storage.Path.exists", return_value=True)
    mocker.patch("mcubridge.state.storage.Path.unlink", side_effect=OSError("Permission denied"))
    mock_logger = mocker.patch("mcubridge.state.storage.logger")

    LmdbDeque(db_path)

    unlink_call = next(
        c for c in mock_logger.warning.call_args_list if c.args and c.args[0] == "Failed to unlink target path"
    )
    assert unlink_call.kwargs["path"] == db_path


async def test_lmdb_cache_recreate_on_corrupt(tmp_path: object, mocker: MockerFixture) -> None:
    db_path = str(tmp_path) + "/cache_test"
    db_error = lmdb.Error("Corrupt DB")

    mock_env = MagicMock()
    mock_env.open_db.return_value = MagicMock()

    mock_open = mocker.patch("lmdb.open", side_effect=[db_error, mock_env])
    mock_logger = mocker.patch("mcubridge.state.storage.logger")

    cache = LmdbCache(db_path)
    await cache.get("test_key")

    assert mock_open.call_count == 2
    mock_logger.warning.assert_any_call(
        "LMDB database corrupt or invalid, recreating",
        path=db_path,
        error=str(db_error),
    )


async def test_lmdb_cache_unlink_os_error(tmp_path: object, mocker: MockerFixture) -> None:
    db_path = str(tmp_path) + "/cache_unlink_test"
    db_error = lmdb.Error("Corrupt DB")

    mock_env = MagicMock()
    mock_env.open_db.return_value = MagicMock()

    mocker.patch("lmdb.open", side_effect=[db_error, mock_env])
    mocker.patch("mcubridge.state.storage.Path.exists", return_value=True)
    mocker.patch("mcubridge.state.storage.Path.unlink", side_effect=OSError("Permission denied"))
    mock_logger = mocker.patch("mcubridge.state.storage.logger")

    LmdbCache(db_path)

    unlink_call = next(
        c for c in mock_logger.warning.call_args_list if c.args and c.args[0] == "Failed to unlink target path"
    )
    assert unlink_call.kwargs["path"] == db_path


async def test_lmdb_cache_uncovered_branches() -> None:
    cache = LmdbCache(path="/tmp/test_cache_branches.db")
    cache.env = None
    assert len(cache) == 0
    assert ("k" in cache) is False
    await cache.set("k", b"val")
    assert await cache.get("k", b"def") == b"def"

    cache.env = MagicMock()
    cache.env.begin.side_effect = lmdb.Error("forced")
    assert len(cache) == 0
    assert ("k" in cache) is False
    assert await cache.get("k", b"def") == b"def"


async def test_corrupt_lmdb_cleanup_unlinks_data_and_lock_files(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Corruption recovery must remove both the data file and its companion -lock file."""
    db_file = tmp_path / "corrupt_with_lock.db"
    lock_file = Path(f"{db_file}-lock")
    db_file.write_bytes(b"NOT A VALID LMDB FILE")
    lock_file.write_bytes(b"STALE CORRUPT LOCK")

    unlinked: list[Path] = []
    real_unlink = Path.unlink

    def spy_unlink(self: Path, missing_ok: bool = False) -> None:
        unlinked.append(self)
        real_unlink(self, missing_ok=missing_ok)

    monkeypatch.setattr(Path, "unlink", spy_unlink)

    dq = LmdbDeque(str(db_file), maxlen=8)
    try:
        assert db_file in unlinked
        assert lock_file in unlinked
        assert dq.env is not None
        await dq.append(b"recovered")
        assert len(dq) == 1
        assert await dq.popleft() == b"recovered"
    finally:
        await dq.close()


async def test_vacuum_reopens_environment_when_replace_fails_after_close(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """If replacing the compacted file fails after env.close(), the caller must reopen cleanly."""
    db_file = tmp_path / "vacuum_fail.db"
    compact_file = Path(f"{db_file}.compact")
    dq = LmdbDeque(str(db_file), maxlen=8)
    try:
        await dq.append(b"entry-1")
        assert len(dq) == 1

        def fail_replace(self: Path, target: Path) -> Path:
            raise OSError(f"Simulated rename failure: {self} -> {target}")

        monkeypatch.setattr(Path, "replace", fail_replace)
        await dq.vacuum()

        assert not compact_file.exists()
        assert dq.env is not None
        assert len(dq) == 1
        assert await dq.peek() == b"entry-1"
        await dq.append(b"entry-2")
        assert await dq.popleft() == b"entry-1"
        assert await dq.popleft() == b"entry-2"
    finally:
        await dq.close()


def test_vacuum_logs_warning_when_reopen_callback_fails(tmp_path: Path, mocker: MockerFixture) -> None:
    """A failing reopen callback in finally must be logged cleanly without escaping."""
    db_file = tmp_path / "reopen_fail.db"
    mock_env = MagicMock()

    def write_compact(dest: str, *, compact: bool = False) -> None:
        _ = compact
        Path(dest).write_bytes(b"compacted")

    mock_env.copy.side_effect = write_compact

    def fail_reopen() -> None:
        raise lmdb.Error("reopen failed")

    mock_logger = mocker.patch("mcubridge.state.storage.logger")
    vacuum_lmdb_env(str(db_file), "deque.db", mock_env, fail_reopen)

    mock_env.close.assert_called_once()
    mock_logger.warning.assert_any_call("Failed to reopen LMDB environment after vacuum", error="reopen failed")
