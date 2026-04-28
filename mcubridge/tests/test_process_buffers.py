import asyncio
from mcubridge.transport.mqtt import MqttTransport
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest
from mcubridge.protocol.protocol import Status
from mcubridge.services.process import ProcessComponent
from mcubridge.services.runtime import BridgeService


@pytest.fixture
def runtime_service(runtime_config: Any, runtime_state: Any) -> BridgeService:
    service = BridgeService(
        runtime_config, runtime_state, MqttTransport(runtime_config, runtime_state)
    )
    return service


@pytest.mark.asyncio
async def test_poll_process_flushes_stored_buffers(
    runtime_service: BridgeService,
) -> None:
    """Test poll_process returns stored buffers and cleans up if finished."""

    pid = 42
    state = runtime_service.state

    mock_handle = MagicMock(spec=asyncio.subprocess.Process)
    mock_handle.stdout = MagicMock()
    mock_handle.stdout.read = AsyncMock(return_value=b"hello")
    mock_handle.stdout.at_eof.side_effect = [False, True, True]
    mock_handle.stderr = MagicMock()
    mock_handle.stderr.read = AsyncMock(return_value=b"world")
    mock_handle.stderr.at_eof.side_effect = [False, True, True]
    mock_handle.returncode = 3

    async with state.process_lock:
        state.running_processes[pid] = mock_handle
        state.process_io_locks[pid] = asyncio.Lock()
        state.process_exit_codes[pid] = 3

    _processonent = runtime_service._container.get(ProcessComponent)  # type: ignore[reportPrivateUsage]
    batch = await _processonent.poll_process(pid)

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
    _processonent = runtime_service._container.get(ProcessComponent)  # type: ignore[reportPrivateUsage]

    # Consume all available slots
    limit = _processonent.state.process_max_concurrent
    for _ in range(limit):
        await _processonent._process_slots.acquire()  # type: ignore[reportPrivateUsage]

    # Try to start another
    pid = await _processonent.run_async("ls")
    assert pid == 0

    # Release all
    for _ in range(limit):
        _processonent._process_slots.release()  # type: ignore[reportPrivateUsage]


@pytest.mark.asyncio
async def test_monitor_process_releases_slot(
    runtime_service: BridgeService,
) -> None:
    _processonent = runtime_service._container.get(ProcessComponent)  # type: ignore[reportPrivateUsage]
    state = runtime_service.state

    pid = 77
    mock_handle = MagicMock(spec=asyncio.subprocess.Process)
    mock_handle.wait = AsyncMock()
    mock_handle.returncode = 5

    io_lock = asyncio.Lock()
    async with state.process_lock:
        state.running_processes[pid] = mock_handle
        state.process_io_locks[pid] = io_lock

    # Save initial value
    initial_value = _processonent._process_slots._value  # type: ignore[reportPrivateUsage]

    # Acquire one slot manually
    await _processonent._process_slots.acquire()  # type: ignore[reportPrivateUsage]

    async with io_lock:
        state.process_exit_codes[pid] = 5
    _processonent._finalize_process_internal(pid)  # type: ignore[reportPrivateUsage]

    assert state.process_exit_codes[pid] == 5

    # Should be back to initial value (because it finalized and released the slot)
    assert _processonent._process_slots._value == initial_value  # type: ignore[reportPrivateUsage]
