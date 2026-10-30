"""Surgical tests for state/status.py, state/storage.py, and state/metrics.py. [SIL-2]"""

from __future__ import annotations

import asyncio
import os
import time
from collections.abc import Iterator
from pathlib import Path
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


async def test_status_writer_periodic_ticks(
    state_setup: tuple[RuntimeState, RuntimeConfig],
) -> None:
    state, _config = state_setup
    task = asyncio.create_task(status_writer(state, interval=1))
    await asyncio.sleep(0.05)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task


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


async def test_lmdb_deque_concurrent_worker_mutations_preserve_length(tmp_path: Path) -> None:
    deque = LmdbDeque(str(tmp_path / "concurrent_deque.db"))
    expected = [f"entry-{index}".encode("ascii") for index in range(24)]

    append_results = await asyncio.gather(*(deque.append(item) for item in expected))
    assert append_results == [0] * len(expected)
    assert len(deque) == len(expected)

    popped = await asyncio.gather(*(deque.popleft() for _ in expected))
    assert sorted(popped) == sorted(expected)
    assert len(deque) == 0
    await deque.close()
    assert len(deque) == 0


async def test_lmdb_deque_popleft_empty_raises(tmp_path: object) -> None:
    db_path = str(tmp_path) + "/empty_deque.db"
    deque = LmdbDeque(db_path)
    with pytest.raises(IndexError):
        await deque.popleft()
    await deque.close()


async def test_lmdb_cache_corruption_recovery(tmp_path: object) -> None:
    db_path = str(tmp_path) + "/corrupt_kv.db"
    with Path(db_path).open("wb") as f:
        f.write(b"GARBAGE")

    kv = LmdbCache(db_path)
    await kv.set("key1", b"value1")
    assert await kv.get("key1") == b"value1"
    assert await kv.get("nonexistent", b"default") == b"default"
    await kv.close()


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


async def test_lmdb_cache_and_vacuum_edge_branches(tmp_path: Path) -> None:
    """Verify fallback and error paths for LmdbCache and _vacuum_lmdb_env."""
    # 1. Vacuum with None env
    await anyio.to_thread.run_sync(_vacuum_lmdb_env, str(tmp_path), "test.db", None, lambda: None)

    db_path = str(tmp_path) + "/edge_branches.db"
    kv = LmdbCache(db_path)
    await kv.set("alpha", b"111")
    assert await kv.get("alpha") == b"111"
    await kv.close()

    # 2. Vacuum unlink OSError
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


async def test_lmdb_cache_len_contains(tmp_path: Path) -> None:
    """Verify LmdbCache __len__ and __contains__ on disk and memory."""
    db_path = str(tmp_path / "cache_ext.db")
    kv = LmdbCache(db_path)

    assert len(kv) == 0
    assert "key1" not in kv

    await kv.set("key1", b"val1")
    await kv.set("key2", b"val2")

    assert len(kv) == 2
    assert "key1" in kv
    assert "key3" not in kv

    # Error and fallback branches with env=None
    saved_env = kv.env
    kv.env = None
    assert len(kv) == 0
    assert "key1" not in kv
    kv.env = saved_env

    # Memory mode verification
    mem_kv = LmdbCache(":memory:")
    assert len(mem_kv) == 0
    assert "mkey" not in mem_kv

    await mem_kv.set("mkey", b"mval")
    assert await mem_kv.get("mkey") == b"mval"
    assert len(mem_kv) == 1
    assert "mkey" in mem_kv
    await mem_kv.close()
    await kv.close()


async def test_open_lmdb_env_dir_branch(tmp_path: Path) -> None:
    cache = LmdbCache(str(tmp_path))
    try:
        await cache.set("k", b"v")
        assert await cache.get("k") == b"v"
    finally:
        await cache.close()


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



async def test_lmdb_closed_resource_operations_are_safe(tmp_path: Path) -> None:
    """Exercise idempotent lifecycle and closed-resource fallbacks."""
    deque = LmdbDeque(str(tmp_path / "closed_deque.db"))
    await deque.append(b"entry")
    await deque.close()
    await deque.close()
    assert len(deque) == 0

    # A closed deque must tolerate cleanup/vacuum calls without reopening LMDB.
    await deque.clear()
    await deque.vacuum()
    assert len(deque) == 0

    cache = LmdbCache(str(tmp_path / "closed_cache.db"))
    await cache.set("before-close", b"value")
    await cache.close()
    await cache.close()
    await cache.set("after-close", b"ignored")
    assert await cache.get("after-close", b"default") == b"default"
    assert cache._get_sync("after-close", b"default") == b"default"
    cache._set_sync("after-close", b"ignored")
    assert len(cache) == 0
    assert "before-close" not in cache


async def test_lmdb_deque_missing_database_handles_all_operations() -> None:
    """The unavailable-environment state is a safe no-op for mutations."""
    deque = LmdbDeque(":memory:")
    deque.is_mem = False
    deque.env = None
    deque.db = None

    assert await deque.append(b"discarded") == 0
    await deque.clear()
    await deque.vacuum()
    await deque.close()
    deque._close_sync()
    assert len(deque) == 0
    with pytest.raises(IndexError, match="popleft from empty deque"):
        await deque.popleft()
    with pytest.raises(IndexError, match="peek from empty deque"):
        await deque.peek()


async def test_lmdb_cache_unavailable_environment_fallback() -> None:
    """Cache read/write and synchronous inspection tolerate a missing environment."""
    cache = LmdbCache(":memory:")
    cache.is_mem = False
    cache.env = None
    cache.db = None

    await cache.set("key", b"value")
    assert await cache.get("key", b"fallback") == b"fallback"
    assert cache._get_sync("key", b"fallback") == b"fallback"
    cache._set_sync("key", b"value")
    assert len(cache) == 0
    assert "key" not in cache
    await cache.close()
    cache._close_sync()
