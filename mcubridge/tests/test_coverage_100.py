"""Tests for various edge cases and coverage gaps (v2)."""

from __future__ import annotations

import asyncio
import collections
from typing import Any, cast
from unittest.mock import MagicMock, AsyncMock, patch

import msgspec
import pytest
from mcubridge.protocol.structures import ProcessPollPacket, ShellCommandPayload
from mcubridge.services.process import ProcessComponent
from mcubridge.services.serial_flow import SerialFlowController
from mcubridge.state.context import RuntimeState


def test_queues_append_basic() -> None:
    # Testing basic append on deque which is now used for RAM queues
    q: collections.deque[bytes] = collections.deque(maxlen=10)
    q.append(b"hello")
    assert len(q) == 1


def test_queues_maxlen_behavior() -> None:
    q: collections.deque[bytes] = collections.deque(maxlen=3)
    q.append(b"1")
    q.append(b"2")
    q.append(b"3")
    q.append(b"4")
    assert len(q) == 3
    assert q.popleft() == b"2"


@pytest.mark.asyncio
async def test_poll_process_not_found_explicit() -> None:
    # Test for coverage of handle_poll when process is missing
    state = MagicMock(spec=RuntimeState)
    state.process_exit_codes = {}
    serial_flow = AsyncMock(spec=SerialFlowController)
    serial_flow.send = AsyncMock()

    comp = ProcessComponent(MagicMock(), state, serial_flow, MagicMock())
    
    from mcubridge.protocol.structures import ShellPidPayload
    payload = msgspec.msgpack.encode(ShellPidPayload(pid=123))
    await comp.handle_poll(1, payload)
    
    serial_flow.send.assert_called()


@pytest.mark.asyncio
async def test_start_async_subprocess_unexpected_exception() -> None:
    # process.py uses create_subprocess_exec
    with patch("asyncio.create_subprocess_exec", side_effect=RuntimeError("fail")):
        serial_flow = AsyncMock(spec=SerialFlowController)
        state = MagicMock(spec=RuntimeState)
        state.process_max_concurrent = 4
        state.allowed_policy = MagicMock()
        state.allowed_policy.is_allowed.return_value = True
        state.process_lock = AsyncMock()
        state.next_pid = 1

        comp = ProcessComponent(MagicMock(), state, serial_flow, MagicMock())
        
        from mcubridge.protocol.structures import ShellCommandPayload
        payload = msgspec.msgpack.encode(ShellCommandPayload(command="ls"))
        await comp.handle_run_async(1, payload)
        # Should proceed without crashing, errors are logged
    
    # Yield control to allow loop cleanup
    await asyncio.sleep(0.01)
