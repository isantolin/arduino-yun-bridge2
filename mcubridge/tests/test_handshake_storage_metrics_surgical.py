"""Surgical unit tests for storage.py, handshake.py, metrics.py, and context.py."""

from __future__ import annotations

import tempfile
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import lmdb
import pytest

from mcubridge.state.context import RuntimeState
from mcubridge.state.storage import LmdbCache, LmdbDeque


@pytest.mark.asyncio
async def test_lmdb_deque_memory_mode() -> None:
    deque = LmdbDeque(":memory:", maxlen=2)
    assert len(deque) == 0
    with pytest.raises(IndexError):
        await deque.popleft()
    with pytest.raises(IndexError):
        await deque.peek()

    await deque.append(b"item1")
    await deque.append(b"item2")
    await deque.append(b"item3")  # Exceeds maxlen=2

    assert len(deque) == 2
    assert await deque.peek() == b"item2"
    assert await deque.popleft() == b"item2"
    assert await deque.popleft() == b"item3"

    await deque.clear()
    assert len(deque) == 0


@pytest.mark.asyncio
async def test_lmdb_deque_no_env_safety() -> None:
    deque = LmdbDeque(":memory:")
    deque.is_mem = False
    deque.env = None

    # Operations when env is None
    await deque.append(b"test")
    with pytest.raises(IndexError):
        await deque.popleft()
    with pytest.raises(IndexError):
        await deque.peek()
    await deque.vacuum()
    await deque.close()


@pytest.mark.asyncio
async def test_lmdb_deque_corrupt_recovery() -> None:
    with tempfile.TemporaryDirectory() as tmpdir:
        db_path = str(Path(tmpdir) / "corrupt_deque.db")
        with patch("lmdb.open", side_effect=lmdb.Error("Database corrupt")):
            deque = LmdbDeque(db_path)
            assert deque.env is None


@pytest.mark.asyncio
async def test_lmdb_cache_basic_and_error_handling() -> None:
    with tempfile.TemporaryDirectory() as tmpdir:
        cache_path = str(Path(tmpdir) / "cache.db")
        cache = LmdbCache(cache_path)

        await cache.set("k1", b"v1")
        assert await cache.get("k1") == b"v1"
        assert await cache.get("nonexistent") is None

        await cache.close()


@pytest.mark.asyncio
async def test_bridge_state_status_pulse() -> None:
    state = RuntimeState()
    state.mark_transport_connected()
    assert state.state == "connected"

    state.mark_synchronized()
    assert state.is_synchronized is True

    state.mark_transport_disconnected()
    assert state.state == "disconnected"
