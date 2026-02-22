"""Final coverage gap closure for ProcessComponent."""

from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from mcubridge.config.settings import RuntimeConfig
from mcubridge.protocol import protocol
from mcubridge.services.process import ProcessComponent
from mcubridge.state.context import ManagedProcess, create_runtime_state


@pytest.mark.asyncio
async def test_process_collect_output_unknown_pid() -> None:
    config = RuntimeConfig(serial_shared_secret=b"secret_1234")
    state = create_runtime_state(config)
    pc = ProcessComponent(config, state, AsyncMock())

    batch = await pc.collect_output(9999) # Unknown PID
    assert batch.status_byte == protocol.Status.ERROR.value
    assert batch.finished is False


@pytest.mark.asyncio
async def test_process_collect_output_race_condition() -> None:
    config = RuntimeConfig(serial_shared_secret=b"secret_1234")
    state = create_runtime_state(config)
    ctx = AsyncMock()
    pc = ProcessComponent(config, state, ctx)

    pid = 100
    slot = ManagedProcess(pid=pid, command="test", handle=MagicMock())
    state.running_processes[pid] = slot

    # Simulate a race condition where the process is removed while io_lock is held
    with patch.object(slot, "pop_payload", return_value=(b"", b"", False, False)):
        # We manually remove the pid from state during the call
        def side_effect(*args, **kwargs):
            state.running_processes.pop(pid, None)
            return (b"", b"", False, False)

        with patch.object(slot, "pop_payload", side_effect=side_effect):
            batch = await pc.collect_output(pid)
            assert batch.status_byte == protocol.Status.ERROR.value


@pytest.mark.asyncio
async def test_process_terminate_tree_no_pid() -> None:
    config = RuntimeConfig(serial_shared_secret=b"secret_1234")
    state = create_runtime_state(config)
    pc = ProcessComponent(config, state, AsyncMock())

    # Use an object that doesn't have a pid attribute
    class NoPidProc:
        def __init__(self):
            self.returncode = None
        def kill(self):
            pass

    mock_proc = MagicMock(spec=NoPidProc)
    mock_proc.returncode = None
    # Ensure getattr(mock_proc, "pid", None) returns None
    del mock_proc.pid

    await pc._terminate_process_tree(mock_proc)
    mock_proc.kill.assert_called()


@pytest.mark.asyncio
async def test_process_kill_lookup_error() -> None:
    config = RuntimeConfig(serial_shared_secret=b"secret_1234")
    state = create_runtime_state(config)
    ctx = AsyncMock()
    pc = ProcessComponent(config, state, ctx)

    pid = 200
    mock_proc = MagicMock()
    slot = ManagedProcess(pid=pid, command="test", handle=mock_proc)
    state.running_processes[pid] = slot

    # Simulate ProcessLookupError during termination
    with patch.object(pc, "_terminate_process_tree", side_effect=ProcessLookupError):
        from mcubridge.protocol.structures import ProcessKillPacket
        payload = ProcessKillPacket(pid=pid).encode()
        await pc.handle_kill(payload)
        # Should catch error and log it, not crash


@pytest.mark.asyncio
async def test_process_finalize_slot_gone() -> None:
    config = RuntimeConfig(serial_shared_secret=b"secret_1234")
    state = create_runtime_state(config)
    pc = ProcessComponent(config, state, AsyncMock())

    mock_proc = MagicMock()
    # Call finalize directly but with a PID that's NOT in state
    await pc._finalize_async_process(300, mock_proc)
    # Should just return gracefully
