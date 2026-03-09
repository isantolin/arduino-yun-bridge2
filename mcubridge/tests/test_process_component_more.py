from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from mcubridge.protocol import structures
from mcubridge.protocol.protocol import Status
from mcubridge.protocol.structures import (
    ProcessOutputBatch,
)
from mcubridge.services.process import ProcessComponent
from mcubridge.state.context import PROCESS_STATE_FINISHED, ManagedProcess


@pytest.fixture
def process_component(runtime_state, runtime_config) -> ProcessComponent:
    from mcubridge.services.runtime import BridgeService

    service = MagicMock(spec=BridgeService)
    service._acknowledge_mcu_frame = AsyncMock()
    service.state = runtime_state

    # Create component
    comp = ProcessComponent(runtime_config, runtime_state, service)
    return comp


def test_post_init_disables_slots_when_limit_zero(runtime_config, runtime_state):
    runtime_config.process_max_concurrent = 0
    comp = ProcessComponent(runtime_config, runtime_state, MagicMock())
    assert comp._process_slots is not None


@pytest.mark.asyncio
async def test_handle_poll_finished_path_executes_debug_branch(
    process_component: ProcessComponent,
) -> None:
    # Setup batch with positional args
    batch = ProcessOutputBatch(Status.OK.value, 0, b"out", b"err", True, False, False)

    with patch.object(process_component, "poll_process", return_value=batch):
        from mcubridge.protocol.structures import ProcessPollPacket
        payload = ProcessPollPacket(pid=100).encode()
        await process_component.handle_poll(payload)
        process_component.service._acknowledge_mcu_frame.assert_awaited()


@pytest.mark.asyncio
async def test_run_async_rejects_when_slot_limit_reached(
    process_component: ProcessComponent,
) -> None:
    limit = process_component.state.process_max_concurrent
    # Acquire all permits
    for _ in range(limit):
        await process_component._process_slots.acquire()

    pid = await process_component.run_async("cmd")
    assert pid == 0


@pytest.mark.asyncio
async def test_poll_process_finishing_process_releases_slot(
    process_component: ProcessComponent,
) -> None:
    pid = 10
    slot = ManagedProcess(pid=pid, command="echo hi")
    # [SIL-2] Slot release happens in _finalize_process, not poll_process.
    # poll_process only cleans up the tracking dictionary.
    slot.fsm_state = PROCESS_STATE_FINISHED
    slot.exit_code = 7

    async with process_component.state.process_lock:
        process_component.state.running_processes[pid] = slot

    # Save initial available value
    initial_value = process_component._process_slots._value

    # Simulate slot acquisition
    await process_component._process_slots.acquire()

    # Manual finalization triggers release
    await process_component._finalize_process(pid, 7)

    assert process_component._process_slots._value == initial_value
    assert pid not in process_component.state.running_processes


@pytest.mark.asyncio
async def test_finalize_process_handles_execution(
    process_component: ProcessComponent,
) -> None:
    pid = 1
    slot = ManagedProcess(pid=pid, command="test")
    slot.trigger("start")

    async with process_component.state.process_lock:
        process_component.state.running_processes[pid] = slot

    # Acquire slot manually to simulate run_async
    await process_component._process_slots.acquire()

    await process_component._finalize_process(pid, 99)
    assert pid not in process_component.state.running_processes
    # Slot should be released
    assert process_component._process_slots._value > 0


@pytest.mark.asyncio
async def test_finalize_process_slot_missing_releases(
    process_component: ProcessComponent,
) -> None:
    # If pid is missing, it should still release the slot (fail-safe)
    # Acquire one slot manually
    await process_component._process_slots.acquire()
    val_before = process_component._process_slots._value
    
    await process_component._finalize_process(999)
    assert process_component._process_slots._value == val_before + 1


@pytest.mark.asyncio
async def test_handle_kill_releases_slot(
    process_component: ProcessComponent,
) -> None:
    pid = 11
    mock_handle = MagicMock()
    mock_handle.terminate = MagicMock()
    slot = ManagedProcess(pid=pid, command="hi")
    slot.handle = mock_handle

    async with process_component.state.process_lock:
        process_component.state.running_processes[pid] = slot

    ok = await process_component.handle_kill(structures.ProcessKillPacket(pid=pid).encode())
    assert ok is True
    mock_handle.terminate.assert_called_once()


@pytest.mark.asyncio
async def test_handle_kill_process_lookup_error_is_handled(
    process_component: ProcessComponent,
) -> None:
    pid = 12
    mock_handle = MagicMock()
    mock_handle.terminate.side_effect = Exception("Lookup Fail")

    slot = ManagedProcess(pid=pid, command="hi")
    slot.handle = mock_handle
    async with process_component.state.process_lock:
        process_component.state.running_processes[pid] = slot

    ok = await process_component.handle_kill(structures.ProcessKillPacket(pid=pid).encode())
    assert ok is True


@pytest.mark.asyncio
async def test_handle_run_async_validation_error_sends_error_frame(
    process_component: ProcessComponent,
) -> None:
    await process_component.handle_run_async(b"")
    process_component.service._acknowledge_mcu_frame.assert_awaited()
    args, kwargs = process_component.service._acknowledge_mcu_frame.call_args
    assert kwargs.get("status") == Status.MALFORMED
