"""Extra unit tests for ProcessComponent branches not covered elsewhere."""

from __future__ import annotations

import struct
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from mcubridge.config.settings import RuntimeConfig
from mcubridge.const import (
    DEFAULT_MQTT_PORT,
    DEFAULT_PROCESS_TIMEOUT,
    DEFAULT_RECONNECT_DELAY,
    DEFAULT_STATUS_INTERVAL,
)
from mcubridge.policy import CommandValidationError
from mcubridge.rpc import protocol
from mcubridge.rpc.protocol import (
    DEFAULT_BAUDRATE as DEFAULT_SERIAL_BAUD,
    DEFAULT_SAFE_BAUDRATE as DEFAULT_SERIAL_SAFE_BAUD,
    Status,
)
from mcubridge.services.components.base import BridgeContext
from mcubridge.services.components.process import ProcessComponent
from mcubridge.state.context import ManagedProcess, create_runtime_state


def _make_config(*, process_max_concurrent: int = 2) -> RuntimeConfig:
    return RuntimeConfig(
        serial_port="/dev/null",
        serial_baud=DEFAULT_SERIAL_BAUD,
        serial_safe_baud=DEFAULT_SERIAL_SAFE_BAUD,
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
        process_max_concurrent=process_max_concurrent,
        serial_shared_secret=b"testsecret",
    )


@pytest.fixture
def mock_context() -> AsyncMock:
    ctx = AsyncMock(spec=BridgeContext)

    async def _schedule(coro, **_kwargs):
        await coro

    ctx.schedule_background.side_effect = _schedule
    ctx.is_command_allowed.return_value = True
    return ctx


@pytest.fixture
def process_component(mock_context: AsyncMock) -> ProcessComponent:
    config = _make_config(process_max_concurrent=2)
    state = create_runtime_state(config)
    return ProcessComponent(config, state, mock_context)


def test_post_init_disables_slots_when_limit_zero(mock_context: AsyncMock) -> None:
    # RuntimeConfig enforces process_max_concurrent > 0; use a config stub
    # to exercise the defensive branch in ProcessComponent.
    config = MagicMock(spec=RuntimeConfig)
    config.process_max_concurrent = 0
    state = MagicMock(spec=create_runtime_state(_make_config()))
    comp = ProcessComponent(config, state, mock_context)  # type: ignore[arg-type]
    assert comp._process_slots is None


@pytest.mark.asyncio
async def test_handle_poll_finished_path_executes_debug_branch(
    process_component: ProcessComponent, mock_context: AsyncMock
) -> None:
    pid = 1
    payload = struct.pack(protocol.UINT16_FORMAT, pid)

    batch = SimpleNamespace(
        status_byte=Status.OK.value,
        exit_code=0,
        stdout_chunk=b"",
        stderr_chunk=b"",
        finished=True,
        stdout_truncated=False,
        stderr_truncated=False,
    )

    with patch.object(ProcessComponent, "collect_output", new_callable=AsyncMock) as mock_collect:
        mock_collect.return_value = batch
        with patch.object(ProcessComponent, "publish_poll_result", new_callable=AsyncMock):
            ok = await process_component.handle_poll(payload)

    assert ok is True
    mock_context.send_frame.assert_awaited_once()


@pytest.mark.asyncio
async def test_run_sync_no_timeout_waits_for_process(process_component: ProcessComponent) -> None:
    process_component.state.process_timeout = 0

    class _EmptyStream:
        async def read(self, _n: int) -> bytes:
            return b""

    class _Proc:
        def __init__(self) -> None:
            self.pid = 42
            self.stdout = _EmptyStream()
            self.stderr = _EmptyStream()
            self.returncode: int | None = None

        async def wait(self) -> None:
            self.returncode = 0

        def kill(self) -> None:
            return None

    proc = _Proc()

    with patch.object(ProcessComponent, "_prepare_command", return_value=("echo", "hi")):
        with patch("asyncio.create_subprocess_exec", return_value=proc):
            status, _out, _err, exit_code = await process_component.run_sync("echo hi")

    assert status == Status.OK.value
    assert exit_code == 0


@pytest.mark.asyncio
async def test_run_sync_taskgroup_exception_returns_error(process_component: ProcessComponent) -> None:
    mock_tg = MagicMock()
    mock_tg.__aenter__ = AsyncMock(return_value=mock_tg)
    mock_tg.create_task.side_effect = RuntimeError("boom")

    with patch("asyncio.TaskGroup", return_value=mock_tg):
        with pytest.raises(RuntimeError, match="boom"):
            await process_component.run_sync("echo hi")


@pytest.mark.asyncio
async def test_run_sync_truncates_and_reports_timeout_flags(process_component: ProcessComponent) -> None:
    process_component.state.process_timeout = 0
    process_component.state.process_output_limit = 3

    class _Stream:
        def __init__(self, chunks: list[bytes]) -> None:
            self._chunks = chunks

        async def read(self, _n: int) -> bytes:
            if not self._chunks:
                return b""
            return self._chunks.pop(0)

    class _Proc:
        def __init__(self) -> None:
            self.pid = 50
            self.stdout = _Stream([b"abcdef", b""])
            self.stderr = _Stream([b"", b""])
            self.returncode: int | None = None

        async def wait(self) -> None:
            self.returncode = 0

        def kill(self) -> None:
            return None

    proc = _Proc()

    with patch.object(ProcessComponent, "_prepare_command", return_value=("echo", "hi")):
        with patch("asyncio.create_subprocess_exec", return_value=proc):
            status, out, err, exit_code = await process_component.run_sync("echo hi")

    assert status == Status.OK.value
    assert out == b"def"
    assert err == b""
    assert exit_code == 0


@pytest.mark.asyncio
async def test_consume_stream_breaks_on_reader_error(process_component: ProcessComponent) -> None:
    class _BadReader:
        async def read(self, _n: int) -> bytes:
            raise OSError("boom")

    buf = bytearray()
    await process_component._consume_stream(1, _BadReader(), buf)  # type: ignore[arg-type]
    assert buf == bytearray()


@pytest.mark.asyncio
async def test_start_async_rejects_when_slot_limit_reached(
    process_component: ProcessComponent,
) -> None:
    with patch.object(ProcessComponent, "_prepare_command", return_value=("echo", "hi")):
        with patch.object(ProcessComponent, "_try_acquire_process_slot", new_callable=AsyncMock) as mock_acquire:
            mock_acquire.return_value = False
            pid = await process_component.start_async("echo hi")

    assert pid == protocol.INVALID_ID_SENTINEL


@pytest.mark.asyncio
async def test_start_async_validation_error_propagates(
    process_component: ProcessComponent,
) -> None:
    with patch.object(ProcessComponent, "_prepare_command", side_effect=CommandValidationError("nope")):
        with pytest.raises(CommandValidationError):
            await process_component.start_async("blocked")


@pytest.mark.asyncio
async def test_collect_output_finishing_process_releases_slot(
    process_component: ProcessComponent,
) -> None:
    pid = 10

    class _Proc:
        def __init__(self) -> None:
            self.pid = 999
            self.returncode = 7
            self.stdout = None
            self.stderr = None

        async def wait(self) -> None:
            return None

        def kill(self) -> None:
            return None

    proc = _Proc()

    slot = ManagedProcess(pid=pid, command="echo hi", handle=proc)

    async with process_component.state.process_lock:
        process_component.state.running_processes[pid] = slot

    with patch.object(ProcessComponent, "_read_process_pipes", new_callable=AsyncMock) as mock_read:
        mock_read.return_value = (b"out", b"err")
        with patch.object(ProcessComponent, "_drain_process_pipes", new_callable=AsyncMock) as mock_drain:
            mock_drain.return_value = (b"", b"")
            with patch.object(ProcessComponent, "_release_process_slot") as mock_release:
                batch = await process_component.collect_output(pid)

    assert batch.exit_code == (7 & protocol.UINT8_MASK)
    assert batch.finished is True
    mock_release.assert_called_once()


@pytest.mark.asyncio
async def test_allocate_pid_exhaustion_returns_sentinel(process_component: ProcessComponent) -> None:
    import mcubridge.services.components.process

    # Shrink the search space so we can exhaust it quickly.
    original_max = mcubridge.services.components.process.UINT16_MAX
    mcubridge.services.components.process.UINT16_MAX = 3
    try:
        async with process_component.state.process_lock:
            process_component.state.next_pid = 1
            process_component.state.running_processes = {
                1: ManagedProcess(pid=1),
                2: ManagedProcess(pid=2),
                3: ManagedProcess(pid=3),
            }
        pid = await process_component._allocate_pid()
        assert pid == protocol.INVALID_ID_SENTINEL
    finally:
        mcubridge.services.components.process.UINT16_MAX = original_max


@pytest.mark.asyncio
async def test_monitor_async_process_handles_wait_exception(process_component: ProcessComponent) -> None:
    class _Proc:
        async def wait(self) -> None:
            raise RuntimeError("boom")

    proc = _Proc()
    with patch.object(ProcessComponent, "_finalize_async_process", new_callable=AsyncMock) as mock_finalize:
        with pytest.raises(RuntimeError, match="boom"):
            await process_component._monitor_async_process(1, proc)  # type: ignore[arg-type]
        mock_finalize.assert_not_awaited()


@pytest.mark.asyncio
async def test_finalize_async_process_slot_missing_releases(process_component: ProcessComponent) -> None:
    proc = SimpleNamespace(returncode=0, stdout=None, stderr=None)
    with patch.object(ProcessComponent, "_release_process_slot") as mock_release:
        await process_component._finalize_async_process(123, proc)  # type: ignore[arg-type]
        mock_release.assert_called_once()


@pytest.mark.asyncio
async def test_handle_kill_timeout_releases_slot(process_component: ProcessComponent) -> None:
    import mcubridge.services.components.process

    pid = 11

    class _Proc:
        def __init__(self) -> None:
            self.returncode: int | None = None

        async def wait(self) -> None:
            return None

    proc = _Proc()
    slot = ManagedProcess(pid=pid, command="echo hi", handle=proc)
    async with process_component.state.process_lock:
        process_component.state.running_processes[pid] = slot

    class _TimeoutCtx:
        async def __aenter__(self) -> None:
            raise TimeoutError

        async def __aexit__(self, _exc_type, _exc, _tb) -> bool:
            return False

    with patch.object(mcubridge.services.components.process.asyncio, "timeout", lambda _timeout: _TimeoutCtx()):
        with patch.object(ProcessComponent, "_terminate_process_tree", new_callable=AsyncMock) as mock_term:
            with patch.object(ProcessComponent, "_release_process_slot") as mock_release:
                ok = await process_component.handle_kill(struct.pack(protocol.UINT16_FORMAT, pid))

    assert ok is True
    mock_term.assert_awaited_once()
    mock_release.assert_called_once()
    async with process_component.state.process_lock:
        assert pid not in process_component.state.running_processes


@pytest.mark.asyncio
async def test_handle_kill_process_lookup_error_is_handled(process_component: ProcessComponent) -> None:
    pid = 12

    class _Proc:
        def __init__(self) -> None:
            self.returncode: int | None = None

        async def wait(self) -> None:
            return None

    proc = _Proc()
    slot = ManagedProcess(pid=pid, command="echo hi", handle=proc)
    async with process_component.state.process_lock:
        process_component.state.running_processes[pid] = slot

    with patch.object(
        ProcessComponent,
        "_terminate_process_tree",
        new_callable=AsyncMock,
        side_effect=ProcessLookupError,
    ) as mock_term:
        with patch.object(ProcessComponent, "_release_process_slot") as mock_release:
            ok = await process_component.handle_kill(struct.pack(protocol.UINT16_FORMAT, pid))

    assert ok is True
    mock_term.assert_awaited_once()
    mock_release.assert_called_once()
    async with process_component.state.process_lock:
        assert pid not in process_component.state.running_processes


@pytest.mark.asyncio
async def test_handle_kill_unexpected_exception_is_handled(process_component: ProcessComponent) -> None:
    pid = 13

    class _Proc:
        def __init__(self) -> None:
            self.returncode: int | None = None

        async def wait(self) -> None:
            return None

    proc = _Proc()
    slot = ManagedProcess(pid=pid, command="echo hi", handle=proc)
    async with process_component.state.process_lock:
        process_component.state.running_processes[pid] = slot

    with patch.object(
        ProcessComponent,
        "_terminate_process_tree",
        new_callable=AsyncMock,
        side_effect=RuntimeError("boom"),
    ):
        with patch.object(ProcessComponent, "_release_process_slot"):
            with pytest.raises(RuntimeError, match="boom"):
                await process_component.handle_kill(struct.pack(protocol.UINT16_FORMAT, pid))
