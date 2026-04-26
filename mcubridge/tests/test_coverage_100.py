"""Tests for various edge cases and coverage gaps (v2)."""

from __future__ import annotations

from typing import Any, cast
from unittest.mock import MagicMock, AsyncMock, patch

import pytest
from mcubridge.state.queues import BridgeQueue
from mcubridge.services.process import ProcessComponent


def test_queues_append_with_bytes_limit_overflow() -> None:
    # max_bytes logic is removed, testing basic append
    q: BridgeQueue[bytes] = BridgeQueue(max_items=10)
    q.append(b"hello")
    assert len(q) == 1


def test_queues_make_room_for_complex() -> None:
    q: BridgeQueue[bytes] = BridgeQueue(max_items=3)
    q.append(b"1")
    q.append(b"2")
    q.append(b"3")
    q.append(b"4")
    assert len(q) == 3
    assert q.popleft() == b"2"


@pytest.mark.asyncio
async def test_poll_process_not_found_explicit() -> None:
    # Test for coverage of poll_process when slot is missing
    state = MagicMock()
    state.process_lock = AsyncMock()
    state.running_processes.get.return_value = None
    serial_flow = MagicMock()
    serial_flow.acknowledge = AsyncMock()
    serial_flow.send = AsyncMock()

    comp = ProcessComponent(MagicMock(), state, serial_flow, MagicMock())
    # handle_poll requires pid and payload in original code
    # We pass a proper seq_id as int to avoid further type errors in internals
    import msgspec
    from mcubridge.protocol.structures import ProcessPollPacket
    payload = msgspec.msgpack.encode(ProcessPollPacket(pid=123))
    await comp.handle_poll(1, payload)


@pytest.mark.asyncio
async def test_finalize_process_slot_gone() -> None:
    comp = ProcessComponent(MagicMock(), MagicMock(), MagicMock(), MagicMock())
    # Signature is (self, pid) in original code
    await cast(Any, comp)._finalize_process(999)


@pytest.mark.asyncio
async def test_start_async_subprocess_unexpected_exception() -> None:
    with patch("asyncio.create_subprocess_shell", side_effect=RuntimeError("fail")):
        serial_flow = MagicMock()
        serial_flow.acknowledge = AsyncMock()
        comp = ProcessComponent(MagicMock(), MagicMock(), serial_flow, MagicMock())
        # handle_run_async signature is (self, seq_id, payload)
        await comp.handle_run_async(1, b"")
