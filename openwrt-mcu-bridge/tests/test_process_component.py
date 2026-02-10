"""Tests for the ProcessComponent."""

from __future__ import annotations

import asyncio
import msgspec
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch
from typing import cast

import pytest
import pytest_asyncio

from mcubridge.config.settings import RuntimeConfig
from mcubridge.config.const import (
    DEFAULT_MQTT_PORT,
    DEFAULT_PROCESS_TIMEOUT,
    DEFAULT_RECONNECT_DELAY,
    DEFAULT_STATUS_INTERVAL,
)
from mcubridge.policy import CommandValidationError
from mcubridge.protocol import protocol
from mcubridge.protocol.protocol import (
    DEFAULT_BAUDRATE,
    DEFAULT_SAFE_BAUDRATE,
    Command,
    Status,
)
from mcubridge.services.base import BridgeContext
from mcubridge.services.process import ProcessComponent, ProcessOutputBatch
from mcubridge.state.context import create_runtime_state


@pytest.fixture
def mock_context() -> AsyncMock:
    ctx = AsyncMock(spec=BridgeContext)

    # Mock schedule_background to just await the coroutine immediately for testing
    async def _schedule(coro):
        await coro

    ctx.schedule_background.side_effect = _schedule
    return ctx


@pytest_asyncio.fixture
async def process_component(mock_context: AsyncMock) -> ProcessComponent:
    config = RuntimeConfig(
        serial_port="/dev/null",
        serial_baud=DEFAULT_BAUDRATE,
        serial_safe_baud=DEFAULT_SAFE_BAUDRATE,
        mqtt_host="localhost",
        mqtt_port=DEFAULT_MQTT_PORT,
        mqtt_user=None,
        mqtt_pass=None,
        mqtt_tls=False,
        mqtt_cafile=None,
        mqtt_certfile=None,
        mqtt_keyfile=None,
        mqtt_topic=protocol.MQTT_DEFAULT_TOPIC_PREFIX,
        allowed_commands=("echo", "ls"),
        file_system_root="/tmp",
        process_timeout=DEFAULT_PROCESS_TIMEOUT,
        reconnect_delay=DEFAULT_RECONNECT_DELAY,
        status_interval=DEFAULT_STATUS_INTERVAL,
        debug_logging=False,
        process_max_concurrent=2,
        serial_shared_secret=b"s_e_c_r_e_t_mock",
    )
    state = create_runtime_state(config)
    return ProcessComponent(config, state, mock_context)


@pytest.mark.asyncio
async def test_handle_run_success(process_component: ProcessComponent, mock_context: AsyncMock) -> None:
    # Mock run_sync to return success
    with patch.object(ProcessComponent, "run_sync", new_callable=AsyncMock) as mock_run:
        mock_run.return_value = (0, b"stdout", b"stderr", 0)

        # Mock _try_acquire_process_slot to return True
        with patch.object(ProcessComponent, "_try_acquire_process_slot", new_callable=AsyncMock) as mock_acquire:
            mock_acquire.return_value = True

            # Mock _build_sync_response
            with patch.object(ProcessComponent, "_build_sync_response") as mock_build:
                mock_build.return_value = b"response_payload"

                await process_component.handle_run(b"echo hello")

                mock_run.assert_awaited_once_with("echo hello", ["echo", "hello"])
                mock_context.send_frame.assert_awaited_once_with(
                    Command.CMD_PROCESS_RUN_RESP.value, b"response_payload"
                )


@pytest.mark.asyncio
async def test_handle_run_limit_reached(process_component: ProcessComponent, mock_context: AsyncMock) -> None:
    # Mock _try_acquire_process_slot to return False
    with patch.object(ProcessComponent, "_try_acquire_process_slot", new_callable=AsyncMock) as mock_acquire:
        mock_acquire.return_value = False

        await process_component.handle_run(b"echo hello")

        mock_context.send_frame.assert_awaited_once()
        args = mock_context.send_frame.call_args[0]
        assert args[0] == Status.ERROR.value
        # Should contain "process_limit_reached" encoded
        assert b"process_limit_reached" in args[1]


@pytest.mark.asyncio
async def test_handle_run_validation_error(process_component: ProcessComponent, mock_context: AsyncMock) -> None:
    # Ensure validation fails
    mock_context.is_command_allowed.return_value = False

    with patch.object(ProcessComponent, "run_sync", new_callable=AsyncMock) as mock_run:
        with patch.object(ProcessComponent, "_try_acquire_process_slot", new_callable=AsyncMock) as mock_acquire:
            mock_acquire.return_value = True

            await process_component.handle_run(b"rm -rf /")

            # run_sync should NOT be called
            mock_run.assert_not_awaited()

            mock_context.send_frame.assert_awaited_once()
            args = mock_context.send_frame.call_args[0]
            assert args[0] == Status.ERROR.value
            # "not allowed" is logged, but the frame contains the status code string
            assert b"command_validation_failed" in args[1]

@pytest.mark.asyncio
async def test_handle_run_async_success(process_component: ProcessComponent, mock_context: AsyncMock) -> None:
    with patch.object(ProcessComponent, "start_async", new_callable=AsyncMock) as mock_start:
        mock_start.return_value = 123

        await process_component.handle_run_async(b"sleep 10")

        mock_start.assert_awaited_once_with("sleep 10", ["sleep", "10"])
        mock_context.send_frame.assert_awaited_once_with(
            Command.CMD_PROCESS_RUN_ASYNC_RESP.value, protocol.UINT16_STRUCT.build(123)
        )
        # Should also enqueue MQTT message
        mock_context.publish.assert_awaited_once()

@pytest.mark.asyncio
async def test_handle_run_async_failure(process_component: ProcessComponent, mock_context: AsyncMock) -> None:
    with patch.object(ProcessComponent, "start_async", new_callable=AsyncMock) as mock_start:
        mock_start.return_value = protocol.INVALID_ID_SENTINEL

        await process_component.handle_run_async(b"fail")

        mock_context.send_frame.assert_awaited_once()
        args = mock_context.send_frame.call_args[0]
        assert args[0] == Status.ERROR.value


@pytest.mark.asyncio
async def test_handle_poll_success(process_component: ProcessComponent, mock_context: AsyncMock) -> None:
    pid = 123
    payload = protocol.UINT16_STRUCT.build(pid)

    batch = ProcessOutputBatch(
        status_byte=1,  # Running
        exit_code=0,
        stdout_chunk=b"out",
        stderr_chunk=b"err",
        finished=False,
        stdout_truncated=False,
        stderr_truncated=False,
    )

    with patch.object(ProcessComponent, "collect_output", new_callable=AsyncMock) as mock_collect:
        mock_collect.return_value = batch
        with patch.object(ProcessComponent, "publish_poll_result", new_callable=AsyncMock):
            await process_component.handle_poll(payload)

            mock_collect.assert_awaited_once_with(pid)
            mock_context.send_frame.assert_awaited_once()
            args = mock_context.send_frame.call_args[0]
            assert args[0] == Command.CMD_PROCESS_POLL_RESP.value
            # Verify payload structure roughly
            resp = args[1]
            assert resp[0] == 1  # status
            assert resp[1] == 0  # exit code
            # lengths
            assert b"out" in resp
            assert b"err" in resp


@pytest.mark.asyncio
async def test_handle_poll_malformed(process_component: ProcessComponent, mock_context: AsyncMock) -> None:
    await process_component.handle_poll(b"1")  # Too short

    mock_context.send_frame.assert_awaited_once()
    args = mock_context.send_frame.call_args[0]
    assert args[0] == Command.CMD_PROCESS_POLL_RESP.value
    assert args[1][0] == Status.MALFORMED.value


@pytest.mark.asyncio
async def test_handle_run_internal_error_sends_error_frame(
    process_component: ProcessComponent, mock_context: AsyncMock
) -> None:
    with patch.object(ProcessComponent, "_try_acquire_process_slot", new_callable=AsyncMock) as mock_acquire:
        mock_acquire.return_value = True

        with patch.object(ProcessComponent, "run_sync", new_callable=AsyncMock) as mock_run:
            mock_run.side_effect = RuntimeError("boom")

            with pytest.raises(RuntimeError, match="boom"):
                await process_component.handle_run(b"echo hello")


@pytest.mark.asyncio
async def test_handle_run_async_validation_error_publishes_error(
    process_component: ProcessComponent, mock_context: AsyncMock
) -> None:
    with patch.object(ProcessComponent, "start_async", new_callable=AsyncMock) as mock_start:
        mock_start.side_effect = CommandValidationError("nope")

        await process_component.handle_run_async(b"blocked")

        assert mock_context.send_frame.await_count == 1
        cmd_id, payload = mock_context.send_frame.call_args[0]
        assert cmd_id == Status.ERROR.value
        assert b"command_validation_failed" in payload

        assert mock_context.publish.await_count == 1
        call_kwargs = mock_context.publish.call_args[1]
        body = msgspec.json.decode(call_kwargs["payload"])
        assert body["status"] == "error"
        assert body["reason"] == "command_validation_failed"


@pytest.mark.asyncio
async def test_process_slot_acquire_timeout_returns_false(
    process_component: ProcessComponent,
) -> None:
    guard = asyncio.BoundedSemaphore(1)
    await guard.acquire()
    process_component._process_slots = guard
    assert await process_component._try_acquire_process_slot() is False


def test_process_slot_release_ignores_value_error(process_component: ProcessComponent) -> None:
    guard = asyncio.BoundedSemaphore(1)
    process_component._process_slots = guard
    # Releasing without a matching acquire raises ValueError; should be swallowed.
    process_component._release_process_slot()


def test_limit_sync_payload_truncates_tail(process_component: ProcessComponent) -> None:
    process_component.state.process_output_limit = 3
    payload, truncated = process_component._limit_sync_payload(b"abcdef")
    assert payload == b"def"
    assert truncated is True


def test_build_sync_response_trims_to_protocol_budget(process_component: ProcessComponent) -> None:
    stdout = b"a" * 9999
    stderr = b"b" * 9999
    out = process_component._build_sync_response(Status.OK.value, stdout, stderr)
    assert out[0] == (Status.OK.value & protocol.UINT8_MASK)
    # Must include two uint16 lengths
    assert len(out) <= protocol.MAX_PAYLOAD_SIZE


@pytest.mark.asyncio
async def test_handle_kill_malformed_payload_returns_false(
    process_component: ProcessComponent,
    mock_context: AsyncMock,
) -> None:
    assert await process_component.handle_kill(protocol.FRAME_DELIMITER, send_ack=True) is False
    mock_context.send_frame.assert_awaited_once_with(
        Status.MALFORMED.value,
        b"process_kill_malformed",
    )


@pytest.mark.asyncio
async def test_handle_kill_unknown_pid_returns_ack(
    process_component: ProcessComponent,
    mock_context: AsyncMock,
) -> None:
    pid = 123
    payload = protocol.UINT16_STRUCT.build(pid)
    assert await process_component.handle_kill(payload, send_ack=True) is True
    mock_context.send_frame.assert_awaited_once_with(
        Status.ERROR.value,
        b"process_not_found",
    )


@pytest.mark.asyncio
async def test_handle_kill_terminates_and_cleans_slot(
    process_component: ProcessComponent, mock_context: AsyncMock
) -> None:
    pid = 77

    class FakeProc:
        def __init__(self) -> None:
            self.pid = 999
            self.returncode: int | None = None

        async def wait(self) -> None:
            # Ensure we hit the timeout path (short sleep for CI performance)
            await asyncio.sleep(0.1)

        def kill(self) -> None:
            return None

    proc = FakeProc()
    # Minimal ManagedProcess-like slot; runtime state stores a richer object, but we only need
    # the attributes used by handle_kill.
    slot = SimpleNamespace(handle=proc, exit_code=None, is_drained=lambda: True)

    async with process_component.state.process_lock:
        process_component.state.running_processes[pid] = slot  # type: ignore[assignment]

    with patch.object(ProcessComponent, "_terminate_process_tree", new_callable=AsyncMock) as mock_term:
        ok = await process_component.handle_kill(
            protocol.UINT16_STRUCT.build(pid),
            send_ack=True,
        )
        assert ok is True
        mock_term.assert_awaited_once()
        mock_context.send_frame.assert_awaited_with(Status.OK.value, b"")

    async with process_component.state.process_lock:
        assert pid not in process_component.state.running_processes


@pytest.mark.asyncio
async def test_terminate_process_tree_short_circuits_when_returned(
    process_component: ProcessComponent,
) -> None:
    proc = SimpleNamespace(returncode=0, pid=123, kill=lambda: None)
    with patch("asyncio.to_thread", new_callable=AsyncMock) as mock_to_thread:
        await process_component._terminate_process_tree(proc)  # type: ignore[arg-type]
        mock_to_thread.assert_not_awaited()


@pytest.mark.asyncio
async def test_terminate_process_tree_kills_when_no_pid(
    process_component: ProcessComponent,
) -> None:
    killed: list[bool] = []

    def _kill() -> None:
        killed.append(True)

    proc = SimpleNamespace(returncode=None, pid=None, kill=_kill)
    await process_component._terminate_process_tree(proc)  # type: ignore[arg-type]
    assert killed


@pytest.mark.asyncio
async def test_run_sync_subprocess_oserror_returns_error(process_component: ProcessComponent) -> None:
    with patch("asyncio.create_subprocess_exec", side_effect=OSError("bad")):
        status, stdout, stderr, exit_code = await process_component.run_sync("/bin/true", ["/bin/true"])
        assert status == Status.ERROR.value
        assert stdout == b""
        assert b"bad" in stderr
        assert exit_code is None


@pytest.mark.asyncio
async def test_run_sync_timeout_kills_process(process_component: ProcessComponent) -> None:
    process_component.state.process_timeout = 1

    class _FakeStream:
        async def read(self, _n: int) -> bytes:
            await asyncio.sleep(0)
            return b""

    class _FakeProc:
        def __init__(self) -> None:
            self.stdout = _FakeStream()
            self.stderr = _FakeStream()
            self.returncode: int | None = None
            self.pid = 123
            self._killed = False

        async def wait(self) -> None:
            # Keep running until killed.
            while not self._killed:
                await asyncio.sleep(0.05)
            self.returncode = 9

        def kill(self) -> None:
            self._killed = True

    fake_proc = _FakeProc()

    with patch("asyncio.create_subprocess_exec", return_value=fake_proc):
        with patch("asyncio.to_thread", new_callable=AsyncMock) as mock_to_thread:
            status, _stdout, _stderr, exit_code = await process_component.run_sync("sleep", ["sleep"])
            assert status == Status.TIMEOUT.value
            assert exit_code == 9
            mock_to_thread.assert_awaited()


@pytest.mark.asyncio
async def test_start_async_allocate_pid_failure_returns_sentinel(process_component: ProcessComponent) -> None:
    with patch.object(ProcessComponent, "_allocate_pid", new_callable=AsyncMock):
        pid = await process_component.start_async("/bin/true", ["/bin/true"])
        assert pid == protocol.INVALID_ID_SENTINEL


@pytest.mark.asyncio
async def test_start_async_subprocess_oserror_returns_sentinel(process_component: ProcessComponent) -> None:
    with patch.object(ProcessComponent, "_allocate_pid", new_callable=AsyncMock) as mock_alloc:
        mock_alloc.return_value = 55
        with patch("asyncio.create_subprocess_exec", side_effect=OSError("bad")):
            pid = await process_component.start_async("/bin/true", ["/bin/true"])
            assert pid == protocol.INVALID_ID_SENTINEL


@pytest.mark.asyncio
async def test_collect_output_unknown_pid_returns_error_batch(
    process_component: ProcessComponent,
) -> None:
    batch = await process_component.collect_output(999)
    assert batch.status_byte == Status.ERROR.value
    assert batch.finished is False


@pytest.mark.asyncio
async def test_read_stream_chunk_timeout_returns_empty(process_component: ProcessComponent) -> None:
    class _SlowReader(asyncio.StreamReader):
        async def read(self, _n: int = -1) -> bytes:  # type: ignore[override]
            await asyncio.sleep(0.05)
            return b"data"

    chunk = await process_component._read_stream_chunk(
        1,
        cast(asyncio.StreamReader, _SlowReader()),
        timeout=0.001,
    )
    assert chunk == b""


@pytest.mark.asyncio
async def test_read_stream_chunk_reader_error_returns_empty(process_component: ProcessComponent) -> None:
    class _BadReader(asyncio.StreamReader):
        async def read(self, _n: int = -1) -> bytes:  # type: ignore[override]
            raise OSError("boom")

    chunk = await process_component._read_stream_chunk(
        1,
        cast(asyncio.StreamReader, _BadReader()),
        timeout=0,
    )
    assert chunk == b""


@pytest.mark.asyncio
async def test_terminate_process_tree_uses_to_thread_when_pid_present(process_component: ProcessComponent) -> None:
    proc = SimpleNamespace(returncode=None, pid=123, kill=lambda: None)
    with patch("asyncio.to_thread", new_callable=AsyncMock) as mock_to_thread:
        await process_component._terminate_process_tree(proc)  # type: ignore[arg-type]
        mock_to_thread.assert_awaited_once()
