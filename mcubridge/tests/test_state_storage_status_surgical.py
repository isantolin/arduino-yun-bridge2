"""Surgical tests for state/status.py, state/storage.py, and state/metrics.py. [SIL-2]"""

from __future__ import annotations

import asyncio
import os
import time
from collections.abc import Iterator
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock

import anyio.to_thread
import lmdb
import mcubridge.state.status as status_mod
import mcubridge.state.storage as storage_mod
import pytest
from hypothesis import given
from hypothesis import strategies as st
from mcubridge.config.settings import RuntimeConfig
from mcubridge.state.context import RuntimeState, create_runtime_state
from mcubridge.state.status import status_writer, write_status_file
from mcubridge.state.storage import LmdbCache, LmdbDeque

_write_status_file = status_mod.write_status_file
_vacuum_lmdb_env = storage_mod.vacuum_lmdb_env


@pytest.fixture
def state_setup(tmp_path: Path) -> Iterator[tuple[RuntimeState, RuntimeConfig]]:

    fs_root = f".tmp_tests/st-fs-{os.getpid()}-{time.time_ns()}"
    spool = f".tmp_tests/st-spool-{os.getpid()}-{time.time_ns()}"
    Path(fs_root).mkdir(exist_ok=True, parents=True)
    Path(spool).mkdir(exist_ok=True, parents=True)
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
    invalid_file = Path("/proc/invalid_status_dir_test/status.json")
    task = asyncio.create_task(status_writer(state, interval=1, status_file=invalid_file))
    await asyncio.sleep(0.05)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task


def test_write_status_file_handles_oserror() -> None:
    from mcubridge.protocol import mcubridge_pb2 as pb

    status_msg = pb.BridgeStatus()
    invalid_file = Path("/proc/invalid_status_dir_test/status.json")
    assert write_status_file(status_msg, status_file=invalid_file) is False


# ---------------------------------------------------------------------------
# storage.py LmdbDeque & LmdbKVStorage tests
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_lmdb_deque_db_recreation_on_corruption(tmp_path: object) -> None:
    db_path = str(tmp_path) + "/corrupt_deque.db"
    # Write garbage to simulate corrupted database
    with Path(db_path).open("wb") as f:
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
    with Path(db_path).open("wb") as f:
        f.write(b"GARBAGE")

    kv = LmdbCache(db_path)
    await kv.set("key1", b"value1")
    assert await kv.get("key1") == b"value1"
    assert await kv.get("nonexistent", b"default") == b"default"
    await kv.clear()
    await kv.close()


@pytest.mark.asyncio
@given(
    maxlen=st.integers(min_value=1, max_value=8),
    items=st.lists(st.binary(min_size=1, max_size=32), min_size=1, max_size=20),
)
async def test_lmdb_deque_multi_overflow_trimming(
    tmp_path_factory: pytest.TempPathFactory, maxlen: int, items: list[bytes]
) -> None:
    """Property: LmdbDeque always retains exactly min(N, maxlen) tail elements in FIFO order."""
    db_path = str(tmp_path_factory.mktemp("deque_overflow")) + "/overflow.db"
    deque = LmdbDeque(db_path, maxlen=maxlen)

    for item in items:
        await deque.append(item)

    expected_len = min(len(items), maxlen)
    assert len(deque) == expected_len

    expected_tail = items[-expected_len:]
    popped = [await deque.popleft() for _ in range(expected_len)]
    assert popped == expected_tail
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
    await anyio.to_thread.run_sync(_vacuum_lmdb_env, str(tmp_path), "test.db", None, lambda: None)

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
    target_dir = tmp_path / "faulty"
    target_dir.mkdir(parents=True, exist_ok=True)
    compact_dir = Path(str(target_dir / "faulty.db") + ".compact")
    compact_dir.mkdir(parents=True, exist_ok=True)
    try:
        await anyio.to_thread.run_sync(_vacuum_lmdb_env, str(target_dir), "faulty.db", faulty_env, lambda: None)
    finally:
        compact_dir.rmdir()


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


@pytest.mark.asyncio
async def test_open_lmdb_env_dir_branch(tmp_path: Path) -> None:
    cache = LmdbCache(str(tmp_path))
    try:
        await cache.set("k", b"v")
        assert await cache.get("k") == b"v"
    finally:
        await cache.close()


@pytest.mark.asyncio
async def test_lmdb_deque_popleft_none_val(tmp_path: Path) -> None:
    db_path = str(tmp_path / "deque.db")
    q = LmdbDeque(db_path)
    try:
        mock_env = MagicMock()
        mock_txn = MagicMock()
        mock_cursor = MagicMock()
        mock_cursor.first.return_value = True
        mock_cursor.key.return_value = b"\x00" * 8
        mock_cursor.pop.return_value = None
        mock_txn.cursor.return_value = mock_cursor
        mock_env.begin.return_value.__enter__.return_value = mock_txn
        q.env = mock_env
        with pytest.raises(IndexError, match="popleft from empty deque"):
            await q.popleft()
    finally:
        await q.close()


@pytest.mark.asyncio
async def test_lmdb_deque_no_env_safety() -> None:
    deque = LmdbDeque(":memory:")
    deque.is_mem = False
    deque.env = None

    await deque.append(b"test")
    with pytest.raises(IndexError):
        await deque.popleft()
    with pytest.raises(IndexError):
        await deque.peek()
    await deque.vacuum()
    await deque.close()
