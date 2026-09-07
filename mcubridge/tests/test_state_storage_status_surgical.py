"""Surgical tests for state/status.py, state/storage.py, and state/metrics.py. [SIL-2]"""

from __future__ import annotations


import asyncio
import os
import time
from collections.abc import Iterator
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock, patch

import lmdb
import pytest

import mcubridge.state.status as status_mod
import mcubridge.state.storage as storage_mod
from mcubridge.config.settings import RuntimeConfig
from mcubridge.state.context import RuntimeState, create_runtime_state
from mcubridge.state.status import status_writer
from mcubridge.state.storage import LmdbCache, LmdbDeque

_write_status_file: Any = getattr(status_mod, "_write_status_file")
_vacuum_lmdb_env: Any = getattr(storage_mod, "_vacuum_lmdb_env")


@pytest.fixture
def state_setup(tmp_path: Path) -> Iterator[tuple[RuntimeState, RuntimeConfig]]:

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
        _write_status_file(status_msg)
        assert mock_file.parent.mkdir.called


# ---------------------------------------------------------------------------
# storage.py LmdbDeque & LmdbKVStorage tests
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_lmdb_deque_db_recreation_on_corruption(tmp_path: object) -> None:
    db_path = str(tmp_path) + "/corrupt_deque.db"
    # Write garbage to simulate corrupted database
    with open(db_path, "wb") as f:
        f.write(b"NOT A VALID LMDB FILE")

    deque = LmdbDeque(db_path, maxlen=10)
    await deque.append(b"item1")
    assert len(deque) == 1
    assert await deque.peek() == b"item1"
    assert await deque.popleft() == b"item1"
    assert len(deque) == 0

    await deque.clear()
    await deque.vacuum()
    await deque.close()


@pytest.mark.asyncio
async def test_lmdb_deque_popleft_empty_raises(tmp_path: object) -> None:
    db_path = str(tmp_path) + "/empty_deque.db"
    deque = LmdbDeque(db_path)
    with pytest.raises(IndexError):
        await deque.popleft()
    await deque.close()


@pytest.mark.asyncio
async def test_lmdb_cache_corruption_recovery(tmp_path: object) -> None:
    db_path = str(tmp_path) + "/corrupt_kv.db"
    with open(db_path, "wb") as f:
        f.write(b"GARBAGE")

    kv = LmdbCache(db_path)
    await kv.set("key1", b"value1")
    assert await kv.get("key1") == b"value1"
    assert await kv.get("nonexistent", b"default") == b"default"
    await kv.clear()
    await kv.close()


@pytest.mark.asyncio
async def test_lmdb_deque_multi_overflow_trimming(tmp_path: object) -> None:
    """Verify LmdbDeque correctly trims multiple overflowing elements without skipping."""
    db_path = str(tmp_path) + "/overflow_deque.db"
    deque = LmdbDeque(db_path, maxlen=2)

    for i in range(5):
        await deque.append(f"item_{i}".encode())

    assert len(deque) == 2
    # Items 0, 1, 2 should be dropped, leaving 3 and 4
    assert await deque.popleft() == b"item_3"
    assert await deque.popleft() == b"item_4"
    assert len(deque) == 0
    await deque.close()


@pytest.mark.asyncio
async def test_lmdb_cache_pop_delete_vacuum_lifecycle(tmp_path: object) -> None:
    """Verify LmdbCache pop, delete, and vacuum operations on disk and memory."""
    db_path = str(tmp_path) + "/kv_ops.db"
    kv = LmdbCache(db_path)

    await kv.set("alpha", b"111")
    await kv.set("beta", b"222")

    # pop existing and default
    assert await kv.pop("alpha") == b"111"
    assert await kv.pop("alpha", b"missing") == b"missing"
    assert await kv.get("alpha") is None

    # delete existing and missing
    assert await kv.delete("beta") is True
    assert await kv.delete("beta") is False

    # vacuum
    await kv.vacuum()
    await kv.close()

    # Memory mode coverage
    mem_kv = LmdbCache(":memory:")
    await mem_kv.set("m1", b"mem_val")
    assert await mem_kv.get("m1") == b"mem_val"
    assert await mem_kv.pop("m1") == b"mem_val"
    assert await mem_kv.pop("m1", b"none") == b"none"
    assert await mem_kv.delete("m1") is False
    await mem_kv.clear()
    await mem_kv.vacuum()
    await mem_kv.close()


@pytest.mark.asyncio
async def test_lmdb_cache_and_vacuum_edge_branches(tmp_path: Path) -> None:
    """Verify fallback and error paths for LmdbCache and _vacuum_lmdb_env."""
    # 1. Vacuum with None env
    _vacuum_lmdb_env(str(tmp_path), "test.db", None, lambda: None)


    db_path = str(tmp_path) + "/edge_branches.db"
    kv = LmdbCache(db_path)

    # 2. None env fallback on pop and delete
    saved_env = kv.env
    kv.env = None
    assert await kv.pop("k", b"def") == b"def"
    assert await kv.delete("k") is False
    kv.env = saved_env

    # 3. LMDB Error on pop and delete
    mock_env = MagicMock()
    mock_env.begin.side_effect = lmdb.Error("Database locked")
    kv.env = mock_env
    assert await kv.pop("k", b"def") == b"def"
    assert await kv.delete("k") is False
    kv.env = saved_env
    await kv.close()

    # 4. Vacuum unlink OSError
    faulty_env = MagicMock()
    faulty_env.copy.side_effect = lmdb.Error("Copy fail")
    with patch("pathlib.Path.unlink", side_effect=OSError("Permission denied")):
        _vacuum_lmdb_env(str(tmp_path / "faulty"), "faulty.db", faulty_env, lambda: None)


@pytest.mark.asyncio
async def test_lmdb_cache_len_contains_items(tmp_path: Path) -> None:
    """Verify LmdbCache __len__, contains, __contains__, and items on disk and memory."""
    db_path = str(tmp_path / "cache_ext.db")
    kv = LmdbCache(db_path)

    assert len(kv) == 0
    assert not await kv.contains("key1")
    assert "key1" not in kv
    assert await kv.items() == []

    await kv.set("key1", b"val1")
    await kv.set("key2", b"val2")

    assert len(kv) == 2
    assert await kv.contains("key1")
    assert "key1" in kv
    assert "key3" not in kv
    assert set(await kv.items()) == {("key1", b"val1"), ("key2", b"val2")}

    # Error and fallback branches with env=None
    saved_env = kv.env
    kv.env = None
    assert len(kv) == 0
    assert not await kv.contains("key1")
    assert "key1" not in kv
    assert await kv.items() == []
    kv.env = saved_env

    # Memory mode verification
    mem_kv = LmdbCache(":memory:")
    assert len(mem_kv) == 0
    assert not await mem_kv.contains("mkey")
    assert "mkey" not in mem_kv
    assert await mem_kv.items() == []

    await mem_kv.set("mkey", b"mval")
    assert len(mem_kv) == 1
    assert await mem_kv.contains("mkey")
    assert "mkey" in mem_kv
    assert await mem_kv.items() == [("mkey", b"mval")]
    await mem_kv.close()
    await kv.close()


