# pyright: reportPrivateUsage=false
"""Surgical tests for state/status.py, state/storage.py, and state/metrics.py. [SIL-2]"""

from __future__ import annotations

import asyncio
import os
import time
from collections.abc import Iterator
from unittest.mock import MagicMock, patch

import pytest

from mcubridge.config.settings import RuntimeConfig
from mcubridge.state.context import RuntimeState, create_runtime_state
from mcubridge.state.status import _write_status_file, status_writer
from mcubridge.state.storage import SqliteCache, SqliteDeque


@pytest.fixture
def state_setup(tmp_path: object) -> Iterator[tuple[RuntimeState, RuntimeConfig]]:
    fs_root = f".tmp_tests/st-fs-{os.getpid()}-{time.time_ns()}"
    spool = f".tmp_tests/st-spool-{os.getpid()}-{time.time_ns()}"
    os.makedirs(fs_root, exist_ok=True)
    os.makedirs(spool, exist_ok=True)
    config = RuntimeConfig(
        file_system_root=fs_root,
        cloud_spool_dir=spool,
        allow_non_tmp_paths=True,
    )
    state = create_runtime_state(config)
    try:
        yield state, config
    finally:
        state.cleanup()


# ---------------------------------------------------------------------------
# status.py tests
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_status_writer_periodic_ticks(
    state_setup: tuple[RuntimeState, RuntimeConfig],
) -> None:
    state, _config = state_setup
    task = asyncio.create_task(status_writer(state, interval=1))
    await asyncio.sleep(0.05)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task


@pytest.mark.asyncio
async def test_status_writer_handles_exception(
    state_setup: tuple[RuntimeState, RuntimeConfig],
) -> None:
    state, _config = state_setup
    with patch("mcubridge.state.status._write_status_file", side_effect=OSError("Disk full")):
        task = asyncio.create_task(status_writer(state, interval=1))
        await asyncio.sleep(0.05)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task


def test_write_status_file_handles_oserror() -> None:
    status_msg = MagicMock()
    with patch("mcubridge.state.status.STATUS_FILE") as mock_file:
        mock_file.parent.mkdir.side_effect = OSError("Access denied")
        # Should catch OSError and log error without raising
        _write_status_file(status_msg)


# ---------------------------------------------------------------------------
# storage.py SqliteDeque & SqliteKVStorage tests
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_sqlite_deque_db_recreation_on_corruption(tmp_path: object) -> None:
    db_path = str(tmp_path) + "/corrupt_deque.db"
    # Write garbage to simulate corrupted database
    with open(db_path, "wb") as f:
        f.write(b"NOT A SQLITE FILE")

    deque = SqliteDeque(db_path, maxlen=10)
    # _execute will fail, trigger _recreate_db(), and succeed
    await deque.append(b"item1")
    assert await deque.length() == 1
    assert await deque.peek() == b"item1"
    assert await deque.popleft() == b"item1"
    assert await deque.length() == 0

    await deque.clear()
    await deque.vacuum()
    await deque.close()


@pytest.mark.asyncio
async def test_sqlite_deque_popleft_empty_raises(tmp_path: object) -> None:
    db_path = str(tmp_path) + "/empty_deque.db"
    deque = SqliteDeque(db_path)
    with pytest.raises(IndexError):
        await deque.popleft()
    await deque.close()


@pytest.mark.asyncio
async def test_sqlite_cache_corruption_recovery(tmp_path: object) -> None:
    db_path = str(tmp_path) + "/corrupt_kv.db"
    with open(db_path, "wb") as f:
        f.write(b"GARBAGE")

    kv = SqliteCache(db_path)
    await kv.set("key1", b"value1")
    assert await kv.get("key1") == b"value1"
    assert await kv.get("nonexistent", b"default") == b"default"
    await kv.clear()
    await kv.close()
