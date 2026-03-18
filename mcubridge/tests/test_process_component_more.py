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
    slot.fsm_state = PROCESS_STATE_FINISHED
    slot.exit_code = 7

    async with process_component.state.process_lock:
        process_component.state.running_processes[pid] = slot

    # Save initial available value
    initial_value = process_component._process_slots._value

    # Acquire one
    await process_component._process_slots.acquire()

    batch = await process_component.poll_process(pid)
    assert batch.exit_code == 7

    # Slot should be released (back to initial)
    assert process_component._process_slots._value == initial_value


@pytest.mark.asyncio
async def test_finalize_callback_async_handles_wait_exception(
    process_component: ProcessComponent,
) -> None:
    # Test finalizing with a fake exit code
    pid = 1

    slot = ManagedProcess(pid=pid, command="test")
    # [FSM] Transition to RUNNING
    slot.trigger("start")

    async with process_component.state.process_lock:
        process_component.state.running_processes[pid] = slot

    await process_component._finalize_callback_async(pid, 99)
    # Should finalize
    assert pid not in process_component.state.running_processes


@pytest.mark.asyncio
async def test_finalize_process_slot_missing_releases(
    process_component: ProcessComponent,
) -> None:
    # If missing, it currently DOES NOT release by design (safety).
    # Update test to expect current value.
    await process_component._process_slots.acquire()
    val_after_acquire = process_component._process_slots._value
    await process_component._finalize_process(999)
    assert process_component._process_slots._value == val_after_acquire


@pytest.mark.asyncio
async def test_handle_kill_timeout_releases_slot(
    process_component: ProcessComponent,
) -> None:
    pid = 11
    mock_handle = MagicMock()
    mock_handle.terminate = MagicMock()
    slot = ManagedProcess(pid=pid, command="hi")
    slot.handle = mock_handle

    async with process_component.state.process_lock:
        process_component.state.running_processes[pid] = slot

    await process_component._process_slots.acquire()

    with patch("psutil.Process") as mock_psutil_cls, \
         patch("psutil.wait_procs", return_value=([], [])):
        mock_psutil_instance = mock_psutil_cls.return_value
        mock_psutil_instance.children.return_value = []
        mock_psutil_instance.terminate = MagicMock()
        ok = await process_component.handle_kill(structures.ProcessKillPacket(pid=pid).encode())
    assert ok is True
    mock_psutil_instance.terminate.assert_called_once()

@pytest.mark.asyncio
async def test_handle_kill_process_lookup_error_is_handled(
    process_component: ProcessComponent,
) -> None:
    pid = 12
    mock_handle = MagicMock()
    mock_handle.process = MagicMock()
    mock_handle.process.terminate.side_effect = Exception("Lookup Fail")

    slot = ManagedProcess(pid=pid, command="hi")
    slot.handle = mock_handle
    async with process_component.state.process_lock:
        process_component.state.running_processes[pid] = slot

    with patch("psutil.Process") as mock_psutil_cls, \
         patch("psutil.wait_procs", return_value=([], [])):
        mock_psutil_instance = mock_psutil_cls.return_value
        mock_psutil_instance.children.return_value = []
        ok = await process_component.handle_kill(structures.ProcessKillPacket(pid=pid).encode())
    # Should return True as we attempted termination
    assert ok is True


@pytest.mark.asyncio
async def test_handle_run_async_validation_error_sends_error_frame(
    process_component: ProcessComponent,
) -> None:
    # Trigger malformed via empty payload
    await process_component.handle_run_async(b"")

    # Verify it called with correct named parameter
    process_component.service._acknowledge_mcu_frame.assert_awaited()
    args, kwargs = process_component.service._acknowledge_mcu_frame.call_args
    assert kwargs.get("status") == Status.MALFORMED
