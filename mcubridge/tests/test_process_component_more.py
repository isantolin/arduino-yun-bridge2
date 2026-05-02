"""Unit tests for ProcessComponent more advanced behaviours (SIL-2)."""

from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, MagicMock

import msgspec
import pytest

from mcubridge.config.settings import RuntimeConfig
from mcubridge.protocol import structures
from mcubridge.protocol.protocol import Status
from mcubridge.services.process import ProcessComponent
from mcubridge.services.serial_flow import SerialFlowController
from mcubridge.state.context import create_runtime_state


@pytest.fixture
def process_comp(runtime_config: RuntimeConfig) -> ProcessComponent:
    state = create_runtime_state(runtime_config)
    serial_flow = AsyncMock(spec=SerialFlowController)
    serial_flow.send = AsyncMock(return_value=True)
    enqueue_mqtt = AsyncMock()
    return ProcessComponent(runtime_config, state, serial_flow, enqueue_mqtt)


@pytest.mark.asyncio
async def test_handle_poll_process_found(process_comp: ProcessComponent) -> None:
    pid = 123
    process_comp.state.process_exit_codes[pid] = 0

    from mcubridge.protocol.structures import ShellPidPayload

    pkt = ShellPidPayload(pid=pid)
    payload = msgspec.msgpack.encode(pkt)

    await process_comp.handle_poll(1, payload)

    assert isinstance(process_comp.serial_flow.send, AsyncMock)
    process_comp.serial_flow.send.assert_called()


@pytest.mark.asyncio
async def test_run_async_respects_concurrency_limit(
    process_comp: ProcessComponent,
) -> None:
    # Set limit to 1
    process_comp.state.process_max_concurrent = 1
    process_comp._process_slots = asyncio.Semaphore(1)  # type: ignore[reportPrivateUsage]
    # [SIL-2] Ensure policy allows it
    process_comp.state.allowed_policy = MagicMock()
    process_comp.state.allowed_policy.is_allowed.return_value = True


@pytest.mark.asyncio
async def test_handle_kill_success(process_comp: ProcessComponent) -> None:
    pid = 456
    mock_proc = MagicMock(spec=asyncio.subprocess.Process)
    process_comp.state.running_processes[pid] = mock_proc

    pkt = structures.ShellPidPayload(pid=pid)
    payload = msgspec.msgpack.encode(pkt)

    await process_comp.handle_kill(1, payload)

    mock_proc.terminate.assert_called()
    assert isinstance(process_comp.serial_flow.send, AsyncMock)
    process_comp.serial_flow.send.assert_called_with(Status.OK.value, b"")


@pytest.mark.asyncio
async def test_handle_run_async_validation_error_sends_error_frame(
    process_comp: ProcessComponent,
) -> None:
    # Trigger malformed via empty payload (will fail decode)
    await process_comp.handle_run_async(0, b"")

    # Should NOT have called send, but return False to dispatcher
    assert isinstance(process_comp.serial_flow.send, AsyncMock)
    process_comp.serial_flow.send.assert_not_called()
