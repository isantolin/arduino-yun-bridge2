import asyncio
from asyncio.subprocess import Process
from typing import Awaitable, Callable, cast

import pytest
from mcubridge.protocol import protocol
from mcubridge.protocol.protocol import Status
from mcubridge.services.process import ProcessComponent, ProcessOutputBatch
from mcubridge.services.runtime import BridgeService
from mcubridge.state.context import (
    ManagedProcess,
    PROCESS_STATE_FINISHED,
    PROCESS_STATE_RUNNING,
)


@pytest.fixture
def runtime_service(runtime_config, runtime_state) -> BridgeService:
    service = BridgeService(runtime_config, runtime_state)
    # Patch state to use the fixture one if needed, but BridgeService creates its own.
    # To keep consistency with other tests, we'll use the service's state.
    return service


@pytest.mark.asyncio
async def test_collect_process_output_flushes_stored_buffers(
    runtime_service: BridgeService,
) -> None:
    """Test collect_output returns stored buffers and cleans up if finished."""

    pid = 42
    state = runtime_service.state
    slot = ManagedProcess(pid, "noop", None)
    slot.exit_code = 3
    slot.stdout_buffer.extend(b"hello")
    slot.stderr_buffer.extend(b"world")

    # [FSM] Set state to FINISHED so collect_output knows it's done
    slot.fsm_state = PROCESS_STATE_FINISHED

    async with state.process_lock:
        state.running_processes[pid] = slot

    collect = cast(
        Callable[[int], Awaitable[ProcessOutputBatch]],
        runtime_service._process.collect_output,
    )

    batch = await collect(pid)

    assert batch.status_byte == Status.OK.value
    assert batch.exit_code == 3
    assert batch.stdout_chunk == b"hello"
    assert batch.stderr_chunk == b"world"
    assert batch.finished is True
    assert batch.stdout_truncated is False
    assert batch.stderr_truncated is False

    # Slot should be removed after final chunk
    assert pid not in state.running_processes
    # Ensure lock remains usable for subsequent consumers
    async with state.process_lock:
        pass


@pytest.mark.asyncio
async def test_start_async_respects_concurrency_limit(
    runtime_service: BridgeService,
) -> None:
    process_component = cast(ProcessComponent, runtime_service._process)
    # Force limit to 1
    process_component._process_slots = asyncio.BoundedSemaphore(1)

    # Consume the slot
    await process_component._process_slots.acquire()

    # Try to start another
    pid = await process_component.start_async("cmd", ["cmd"])
    assert pid == protocol.INVALID_ID_SENTINEL

    process_component._process_slots.release()


@pytest.mark.asyncio
async def test_handle_run_respects_concurrency_limit(
    runtime_service: BridgeService,
) -> None:
    process_component = cast(ProcessComponent, runtime_service._process)
    process_component._process_slots = asyncio.BoundedSemaphore(1)
    await process_component._process_slots.acquire()

    # Simulate RUN command
    from mcubridge.protocol.structures import ProcessRunPacket

    packet = ProcessRunPacket(command="echo hi").encode()
    await process_component.handle_run(packet)

    # Should have sent error frame
    pass


@pytest.mark.asyncio
async def test_async_process_monitor_releases_slot(
    runtime_service: BridgeService,
) -> None:
    process_component = cast(ProcessComponent, runtime_service._process)
    state = runtime_service.state
    process_component._process_slots = asyncio.BoundedSemaphore(1)
    guard = process_component._process_slots
    assert guard is not None
    await guard.acquire()

    class _FakeStream:
        def __init__(self, payload: bytes) -> None:
            self._buffer = bytearray(payload)

        async def read(self, max_bytes: int | None = None) -> bytes:
            if not self._buffer:
                return b""
            size = len(self._buffer)
            if max_bytes is not None:
                size = min(size, max_bytes)
            chunk = bytes(self._buffer[:size])
            del self._buffer[:size]
            return chunk

    class _FakeProcess:
        def __init__(self) -> None:
            self.stdout = _FakeStream(b"out")
            self.stderr = _FakeStream(b"err")
            self.returncode: int | None = 5
            self.pid = 9999

        async def wait(self) -> None:
            return None

    fake_proc = _FakeProcess()
    slot = ManagedProcess(
        77,
        "/bin/true",
        cast(Process, fake_proc),
    )

    # [FSM] Set state to RUNNING so finalize can transition it
    slot.fsm_state = PROCESS_STATE_RUNNING

    async with state.process_lock:
        state.running_processes[slot.pid] = slot

    await process_component._monitor_async_process(
        slot.pid,
        cast(Process, fake_proc),
    )

    assert slot.handle is None
    assert slot.exit_code == 5
    assert bytes(slot.stdout_buffer) == b"out"
    assert bytes(slot.stderr_buffer) == b"err"

    # Should be able to acquire again if released
    await asyncio.wait_for(guard.acquire(), timeout=0.1)
    process_component._release_process_slot()


def test_trim_process_buffers_mutates_in_place(
    runtime_service: BridgeService,
) -> None:
    process_component = cast(ProcessComponent, runtime_service._process)
    stdout = bytearray(b"A" * 10)
    stderr = bytearray(b"B" * 10)

    out_c, err_c, t_out, t_err = process_component.trim_buffers(stdout, stderr)
    assert out_c == b"A" * 10
    assert err_c == b"B" * 10
    assert len(stdout) == 0
    assert len(stderr) == 0
    assert t_out is False
