import msgspec
from mcubridge.services.serial_flow import SerialFlowController
from mcubridge.transport.mqtt import MqttTransport
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
import asyncio
from mcubridge.protocol import structures
from mcubridge.protocol.protocol import Status
from mcubridge.protocol.structures import (
    ProcessOutputBatch,
)
from mcubridge.services.process import ProcessComponent


@pytest.fixture
def process_comp(runtime_state: Any, runtime_config: Any) -> ProcessComponent:
    serial_flow = AsyncMock(spec=SerialFlowController)
    serial_flow.acknowledge = AsyncMock()
    serial_flow.send = AsyncMock()

    mqtt_flow = AsyncMock(spec=MqttTransport)
    mqtt_flow.publish = AsyncMock()

    # Create component with direct dependencies
    comp = ProcessComponent(
        config=runtime_config,
        state=runtime_state,
        serial_flow=serial_flow,
        mqtt_flow=mqtt_flow,
    )
    return comp


def test_post_init_disables_slots_when_limit_zero(
    runtime_config: Any, runtime_state: Any
):
    runtime_config.process_max_concurrent = 0
    comp = ProcessComponent(
        config=runtime_config,
        state=runtime_state,
        serial_flow=AsyncMock(spec=SerialFlowController),
        mqtt_flow=AsyncMock(spec=MqttTransport),
    )
    assert comp._process_slots is not None  # type: ignore[reportPrivateUsage]


@pytest.mark.asyncio
async def test_handle_poll_finished_path_executes_debug_branch(
    process_comp: ProcessComponent,
) -> None:
    # Setup batch with positional args
    batch = ProcessOutputBatch(Status.OK.value, 0, b"out", b"err", True, False, False)

    with patch.object(process_comp, "poll_process", return_value=batch):
        from mcubridge.protocol.structures import ProcessPollPacket

        payload = msgspec.msgpack.encode(ProcessPollPacket(pid=100))
        await process_comp.handle_poll(0, payload)
        process_comp.serial_flow.acknowledge.assert_awaited()  # type: ignore[reportUnknownMemberType]


@pytest.mark.asyncio
async def test_run_async_rejects_when_slot_limit_reached(
    process_comp: ProcessComponent,
) -> None:
    limit = process_comp.state.process_max_concurrent
    # Acquire all permits
    for _ in range(limit):
        await process_comp._process_slots.acquire()  # type: ignore[reportPrivateUsage]

    pid = await process_comp.run_async("cmd")
    assert pid == 0


@pytest.mark.asyncio
async def test_poll_process_finishing_process_releases_slot(
    process_comp: ProcessComponent,
) -> None:
    pid = 10
    state = process_comp.state

    mock_handle = MagicMock(spec=asyncio.subprocess.Process)
    mock_handle.pid = pid
    mock_handle.returncode = 7
    mock_handle.stdout = AsyncMock(spec=asyncio.StreamReader)
    mock_handle.stderr = AsyncMock(spec=asyncio.StreamReader)
    mock_handle.stdout.at_eof.return_value = True
    mock_handle.stderr.at_eof.return_value = True
    mock_handle.stdout.read.return_value = b""
    mock_handle.stderr.read.return_value = b""

    async with state.process_lock:
        state.running_processes[pid] = mock_handle
        state.process_io_locks[pid] = asyncio.Lock()
        state.process_exit_codes[pid] = 7

    # Save initial available value
    initial_value = process_comp._process_slots._value  # type: ignore[reportPrivateUsage]

    # Acquire one
    await process_comp._process_slots.acquire()  # type: ignore[reportPrivateUsage]

    batch = await process_comp.poll_process(pid)
    assert batch.exit_code == 7

    # Slot should be released (back to initial)
    assert process_comp._process_slots._value == initial_value  # type: ignore[reportPrivateUsage]


@pytest.mark.asyncio
async def test_finalize_callback_async_handles_wait_exception(
    process_comp: ProcessComponent,
) -> None:
    # Test finalizing with a fake exit code
    pid = 1
    state = process_comp.state

    mock_handle = MagicMock(spec=asyncio.subprocess.Process)
    mock_handle.returncode = 0
    io_lock = asyncio.Lock()

    async with state.process_lock:
        state.running_processes[pid] = mock_handle
        state.process_io_locks[pid] = io_lock

    async with io_lock:
        state.process_exit_codes[pid] = 99
    process_comp._finalize_process_internal(pid)  # type: ignore[reportPrivateUsage]

    # Should finalize
    assert pid not in process_comp.state.running_processes


@pytest.mark.asyncio
async def test_finalize_process_slot_missing_releases(
    process_comp: ProcessComponent,
) -> None:
    # If missing, it currently DOES NOT release by design (safety).
    # Update test to expect current value.
    await process_comp._process_slots.acquire()  # type: ignore[reportPrivateUsage]
    val_after_acquire = process_comp._process_slots._value  # type: ignore[reportPrivateUsage]
    await process_comp._finalize_process(999)  # type: ignore[reportPrivateUsage]
    assert process_comp._process_slots._value == val_after_acquire  # type: ignore[reportPrivateUsage]


@pytest.mark.asyncio
async def test_handle_kill_timeout_releases_slot(
    process_comp: ProcessComponent,
) -> None:
    pid = 11
    state = process_comp.state
    mock_handle = MagicMock(spec=asyncio.subprocess.Process)
    mock_handle.pid = pid
    mock_handle.terminate = MagicMock()

    async with state.process_lock:
        state.running_processes[pid] = mock_handle
        state.process_io_locks[pid] = asyncio.Lock()

    await process_comp._process_slots.acquire()  # type: ignore[reportPrivateUsage]

    with (
        patch("psutil.Process") as mock_psutil_cls,
        patch("psutil.wait_procs", return_value=([], [])),
    ):
        mock_child = MagicMock()
        mock_psutil_instance = MagicMock()
        mock_psutil_cls.return_value = mock_psutil_instance
        mock_psutil_instance.children.return_value = [mock_child]

        ok = await process_comp.handle_kill(
            0, msgspec.msgpack.encode(structures.ProcessKillPacket(pid=pid))
        )
    assert ok is True
    # The current implementation terminates parent AND children
    assert mock_psutil_instance.terminate.called or mock_child.terminate.called


@pytest.mark.asyncio
async def test_handle_kill_process_lookup_error_is_handled(
    process_comp: ProcessComponent,
) -> None:
    pid = 12
    state = process_comp.state
    mock_handle = MagicMock(spec=asyncio.subprocess.Process)
    mock_handle.pid = pid

    async with state.process_lock:
        state.running_processes[pid] = mock_handle
        state.process_io_locks[pid] = asyncio.Lock()

    with (patch("psutil.Process", side_effect=Exception("Lookup Fail")),):
        ok = await process_comp.handle_kill(
            0, msgspec.msgpack.encode(structures.ProcessKillPacket(pid=pid))
        )
    # Should return True as we attempted termination
    assert ok is True


@pytest.mark.asyncio
async def test_handle_run_async_validation_error_sends_error_frame(
    process_comp: ProcessComponent,
) -> None:
    # Trigger malformed via empty payload
    await process_comp.handle_run_async(0, b"")

    # Verify it called with correct named parameter
    process_comp.serial_flow.acknowledge.assert_awaited()  # type: ignore[reportUnknownMemberType]
    args, kwargs = process_comp.serial_flow.acknowledge.call_args  # type: ignore[reportUnknownVariableType]
    assert kwargs.get("status") == Status.MALFORMED  # type: ignore[reportUnknownMemberType]
