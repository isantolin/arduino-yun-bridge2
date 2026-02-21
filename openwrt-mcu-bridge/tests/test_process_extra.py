"""Extra coverage for mcubridge.services.process."""

import asyncio
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
import psutil
from mcubridge.config.settings import RuntimeConfig
from mcubridge.protocol.protocol import INVALID_ID_SENTINEL, Status
from mcubridge.services.process import ProcessComponent
from mcubridge.state.context import create_runtime_state


@pytest.mark.asyncio
async def test_process_handle_run_malformed() -> None:
    config = RuntimeConfig(serial_shared_secret=b"secret_1234")
    state = create_runtime_state(config)
    ctx = MagicMock()
    ctx.send_frame = AsyncMock()
    pc = ProcessComponent(config, state, ctx)
    await pc.handle_run(b"")
    ctx.send_frame.assert_called()
    assert ctx.send_frame.call_args[0][0] == Status.MALFORMED.value


@pytest.mark.asyncio
async def test_process_handle_run_async_malformed() -> None:
    config = RuntimeConfig(serial_shared_secret=b"secret_1234")
    state = create_runtime_state(config)
    ctx = MagicMock()
    ctx.send_frame = AsyncMock()
    pc = ProcessComponent(config, state, ctx)
    await pc.handle_run_async(b"")
    assert ctx.send_frame.call_args[0][0] == Status.MALFORMED.value


@pytest.mark.asyncio
async def test_process_handle_run_async_fail_sentinel() -> None:
    config = RuntimeConfig(serial_shared_secret=b"secret_1234")
    state = create_runtime_state(config)
    ctx = MagicMock()
    ctx.send_frame = AsyncMock()
    ctx.publish = AsyncMock()
    pc = ProcessComponent(config, state, ctx)

    from mcubridge.protocol.structures import ProcessRunAsyncPacket
    payload = ProcessRunAsyncPacket(command="ls").encode()

    # Patch the class method because msgspec Struct instances are read-only
    with patch("mcubridge.services.process.ProcessComponent.start_async", return_value=INVALID_ID_SENTINEL):
        await pc.handle_run_async(payload)
        assert ctx.send_frame.call_args[0][0] == Status.ERROR.value


@pytest.mark.asyncio
async def test_process_run_sync_oserror() -> None:
    config = RuntimeConfig(serial_shared_secret=b"secret_1234")
    state = create_runtime_state(config)
    pc = ProcessComponent(config, state, MagicMock())

    with patch("asyncio.create_subprocess_exec", side_effect=OSError("fail")):
        status, out, err, code = await pc.run_sync("ls", ["ls"])
        assert status == Status.ERROR.value
        assert b"fail" in err


@pytest.mark.asyncio
async def test_process_start_async_oserror() -> None:
    config = RuntimeConfig(serial_shared_secret=b"secret_1234")
    state = create_runtime_state(config)
    pc = ProcessComponent(config, state, MagicMock())

    with patch("asyncio.create_subprocess_exec", side_effect=OSError("fail")):
        pid = await pc.start_async("ls", ["ls"])
        assert pid == INVALID_ID_SENTINEL


@pytest.mark.asyncio
async def test_process_collect_output_unknown_pid() -> None:
    config = RuntimeConfig(serial_shared_secret=b"secret_1234")
    state = create_runtime_state(config)
    pc = ProcessComponent(config, state, MagicMock())

    batch = await pc.collect_output(999)
    assert batch.status_byte == Status.ERROR.value


@pytest.mark.asyncio
async def test_process_read_stream_chunk_exceptions() -> None:
    config = RuntimeConfig(serial_shared_secret=b"secret_1234")
    state = create_runtime_state(config)
    pc = ProcessComponent(config, state, MagicMock())

    reader = MagicMock()
    reader.read = AsyncMock(side_effect=asyncio.IncompleteReadError(b"", 10))

    res = await pc._read_stream_chunk(1, reader, timeout=0)
    assert res == b""


@pytest.mark.asyncio
async def test_process_run_sync_taskgroup_oserror() -> None:
    config = RuntimeConfig(serial_shared_secret=b"secret_1234")
    state = create_runtime_state(config)
    pc = ProcessComponent(config, state, MagicMock())

    with patch("mcubridge.services.process.ProcessComponent._consume_stream", side_effect=OSError("io error")):
        status, out, err, code = await pc.run_sync("ls", ["ls"])
        assert status == Status.ERROR.value
        assert b"IO error" in err


@pytest.mark.asyncio
async def test_process_wait_sync_completion_timeout() -> None:
    config = RuntimeConfig(serial_shared_secret=b"secret_1234")
    state = create_runtime_state(config)
    state.process_timeout = 0.1
    pc = ProcessComponent(config, state, MagicMock())

    proc = MagicMock()
    proc.wait = AsyncMock(side_effect=asyncio.TimeoutError)

    with patch("mcubridge.services.process.ProcessComponent._terminate_process_tree", return_value=None):
        res = await pc._wait_for_sync_completion(proc, 123)
        assert res is True


@pytest.mark.asyncio
async def test_process_read_stream_chunk_timeout() -> None:
    config = RuntimeConfig(serial_shared_secret=b"secret_1234")
    state = create_runtime_state(config)
    pc = ProcessComponent(config, state, MagicMock())

    reader = MagicMock()
    with patch("asyncio.wait_for", side_effect=asyncio.TimeoutError):
        res = await pc._read_stream_chunk(1, reader, timeout=0.1)
        assert res == b""


@pytest.mark.asyncio
async def test_process_terminate_tree_already_finished() -> None:
    config = RuntimeConfig(serial_shared_secret=b"secret_1234")
    state = create_runtime_state(config)
    pc = ProcessComponent(config, state, MagicMock())
    proc = MagicMock()
    proc.returncode = 0
    await pc._terminate_process_tree(proc)


@pytest.mark.asyncio
async def test_process_terminate_tree_no_pid() -> None:
    config = RuntimeConfig(serial_shared_secret=b"secret_1234")
    state = create_runtime_state(config)
    pc = ProcessComponent(config, state, MagicMock())
    # Restrict spec so 'pid' attribute does not exist
    proc = MagicMock(spec=["returncode", "kill", "wait"])
    proc.returncode = None
    await pc._terminate_process_tree(proc)
    proc.kill.assert_called()


@pytest.mark.asyncio
async def test_process_finalize_missing_slot() -> None:
    config = RuntimeConfig(serial_shared_secret=b"secret_1234")
    state = create_runtime_state(config)
    pc = ProcessComponent(config, state, MagicMock())

    proc = MagicMock()
    await pc._finalize_async_process(999, proc)


@pytest.mark.asyncio
async def test_process_allocate_pid_exhaustion() -> None:
    config = RuntimeConfig(serial_shared_secret=b"secret_1234")
    state = create_runtime_state(config)
    pc = ProcessComponent(config, state, MagicMock())

    class FullDict(dict):
        def __contains__(self, item):
            return True

    with patch.object(state, "running_processes", FullDict()):
        pid = await pc._allocate_pid()
        assert pid == INVALID_ID_SENTINEL


def test_process_kill_tree_sync_psutil_error() -> None:
    with patch("psutil.Process", side_effect=psutil.NoSuchProcess(1)):
        ProcessComponent._kill_process_tree_sync(1)
