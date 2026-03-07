from typing import cast
from unittest.mock import AsyncMock, MagicMock

import pytest
from mcubridge.protocol.protocol import Status
from mcubridge.services.process import ProcessComponent
from mcubridge.services.runtime import BridgeService
from mcubridge.state.context import (
    PROCESS_STATE_FINISHED,
    ManagedProcess,
)


@pytest.fixture
def runtime_service(runtime_config, runtime_state) -> BridgeService:
    service = BridgeService(runtime_config, runtime_state)
    return service


@pytest.mark.asyncio
async def test_poll_process_flushes_stored_buffers(
    runtime_service: BridgeService,
) -> None:
    """Test poll_process returns stored buffers and cleans up if finished."""

    pid = 42
    state = runtime_service.state
    slot = ManagedProcess(pid, "noop")
    slot.exit_code = 3
    slot.stdout_buffer.extend(b"hello")
    slot.stderr_buffer.extend(b"world")

    # [FSM] Set state to FINISHED so poll_process knows it's done
    slot.fsm_state = PROCESS_STATE_FINISHED

    async with state.process_lock:
        state.running_processes[pid] = slot

    process_component = cast(ProcessComponent, runtime_service._process)
    batch = await process_component.poll_process(pid)

    # ProcessOutputBatch fields: status_byte, exit_code, stdout_chunk, stderr_chunk, finished, ...
    assert batch.status_byte == Status.OK.value
    assert batch.exit_code == 3
    assert batch.stdout_chunk == b"hello"
    assert batch.stderr_chunk == b"world"

    # Slot should be removed after final chunk
    async with state.process_lock:
        assert pid not in state.running_processes


@pytest.mark.asyncio
async def test_run_async_respects_concurrency_limit(
    runtime_service: BridgeService,
) -> None:
    process_component = cast(ProcessComponent, runtime_service._process)

    # Consume all available slots
    limit = process_component.state.process_max_concurrent
    for _ in range(limit):
        await process_component._process_slots.acquire()

    # Try to start another
    pid = await process_component.run_async("ls")
    assert pid == 0

    # Release all
    for _ in range(limit):
        process_component._process_slots.release()


@pytest.mark.asyncio
async def test_monitor_process_releases_slot(
    runtime_service: BridgeService,
) -> None:
    process_component = cast(ProcessComponent, runtime_service._process)
    state = runtime_service.state

    mock_handle = MagicMock()
    mock_handle.wait = AsyncMock()
    mock_handle.returncode = 5

    slot = ManagedProcess(
        77,
        "/bin/true",
    )
    slot.handle = mock_handle
    # [FSM] Transition to RUNNING
    slot.trigger("start")

    async with state.process_lock:
        state.running_processes[77] = slot

    # Save initial value
    initial_value = process_component._process_slots._value

    # Acquire one slot manually
    await process_component._process_slots.acquire()

    await process_component._finalize_callback_async(77, 5)

    assert slot.exit_code == 5

    # Should be back to initial value (because it finalized and released the slot)
    assert process_component._process_slots._value == initial_value
