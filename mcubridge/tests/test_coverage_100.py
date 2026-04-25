"""Tests for various edge cases and coverage gaps (v2)."""

from unittest.mock import MagicMock, patch

import pytest
from mcubridge.state.queues import BridgeQueue
from mcubridge.services.process import ProcessComponent


def test_queues_append_with_bytes_limit_overflow():
    # max_bytes logic is removed, testing basic append
    q = BridgeQueue[bytes](max_items=10)
    q.append(b"hello")
    assert len(q) == 1


def test_queues_make_room_for_complex():
    q = BridgeQueue[bytes](max_items=3)
    q.append(b"1")
    q.append(b"2")
    q.append(b"3")
    q.append(b"4")
    assert len(q) == 3
    assert q.popleft() == b"2"


@pytest.mark.asyncio
async def test_poll_process_not_found_explicit():
    # Test for coverage of poll_process when slot is missing
    state = MagicMock()
    comp = ProcessComponent(MagicMock(), state, MagicMock())
    comp._process_slots = {}
    await comp.handle_poll(MagicMock(pid=999))
    # Should just return without error


@pytest.mark.asyncio
async def test_finalize_process_slot_gone():
    comp = ProcessComponent(MagicMock(), MagicMock(), MagicMock())
    comp._process_slots = {}
    await comp._finalize_process(999, MagicMock())
    # Should handle missing slot gracefully


@pytest.mark.asyncio
async def test_start_async_subprocess_unexpected_exception():
    with patch("asyncio.create_subprocess_shell", side_effect=RuntimeError("fail")):
        comp = ProcessComponent(MagicMock(), MagicMock(), MagicMock())
        await comp.handle_run_async(MagicMock(command="ls"))
        # Should catch and log
