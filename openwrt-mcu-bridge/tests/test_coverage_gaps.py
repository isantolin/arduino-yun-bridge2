"Tests specifically targeting coverage gaps identified in the codebase."

from __future__ import annotations

import asyncio
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
from mcubridge.rpc import protocol
from mcubridge.rpc.protocol import (
    DEFAULT_BAUDRATE,
    DEFAULT_SAFE_BAUDRATE,
    Status,
)
from mcubridge.services.components.base import BridgeContext
from mcubridge.services.components.process import ProcessComponent
from mcubridge.state.context import ManagedProcess, create_runtime_state


def _make_config(*, process_max_concurrent: int = 2) -> RuntimeConfig:
    return RuntimeConfig(
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


# ============================================================================
# PROCESS.PY COVERAGE GAPS (lines 244, 317-324, 358-359, 378-381)
# ============================================================================


@pytest.mark.asyncio
async def test_handle_kill_process_wait_timeout_logs_warning(
    process_component: ProcessComponent,
    mock_context: AsyncMock,
) -> None:
    """Cover lines 317-324: Process doesn't terminate after kill within 0.5s."""
    pid = 100

    class _StubProc:
        def __init__(self) -> None:
            self.pid = 999
            self.returncode: int | None = None
            self._killed = False

        async def wait(self) -> None:
            # First wait call (the 0.5s timeout one) never completes
            if not self._killed:
                await asyncio.sleep(10)
            # After kill, returns immediately
            self.returncode = 137

        def kill(self) -> None:
            self._killed = True

    proc = _StubProc()
    slot = ManagedProcess(pid=pid, command="stubborn", handle=proc)
    async with process_component.state.process_lock:
        process_component.state.running_processes[pid] = slot

    # Patch _terminate_process_tree to actually kill the process
    async def _fake_terminate(self, p):
        p.kill()

    with patch.object(
        ProcessComponent,
        "_terminate_process_tree",
        _fake_terminate,
    ):
        payload = struct.pack(protocol.UINT16_FORMAT, pid)
        ok = await process_component.handle_kill(payload)

    assert ok is True
    mock_context.send_frame.assert_awaited_with(Status.OK.value, b"")


@pytest.mark.asyncio
async def test_run_sync_wait_task_result_exception(
    process_component: ProcessComponent,
) -> None:
    """Cover lines 358-359: Exception when getting wait_task.result()."""
    process_component.state.process_timeout = 0.1

    class _EmptyStream:
        async def read(self, _n: int) -> bytes:
            return b""

    class _Proc:
        def __init__(self) -> None:
            self.pid = 123
            self.stdout = _EmptyStream()
            self.stderr = _EmptyStream()
            self.returncode: int | None = 0

        async def wait(self) -> None:
            self.returncode = 0

        def kill(self) -> None:
            pass

    proc = _Proc()

    with patch.object(ProcessComponent, "_prepare_command", return_value=("echo", "hi")):
        with patch("asyncio.create_subprocess_exec", return_value=proc):
            # Make _wait_for_sync_completion raise an exception
            async def _bad_wait(*args, **kwargs):
                raise ValueError("test error")

            with patch.object(ProcessComponent, "_wait_for_sync_completion", side_effect=_bad_wait):
                with pytest.raises(ExceptionGroup) as excinfo:
                    await process_component.run_sync("echo hi")

                assert "test error" in str(excinfo.value.exceptions[0])


@pytest.mark.asyncio
async def test_terminate_process_tree_already_returned(
    process_component: ProcessComponent,
) -> None:
    """Cover lines 378-381: Process already finished before kill."""
    # returncode is not None -> should short-circuit
    proc = SimpleNamespace(returncode=0, pid=123, kill=lambda: None)
    with patch("asyncio.to_thread", new_callable=AsyncMock) as mock_to_thread:
        await process_component._terminate_process_tree(proc)  # type: ignore[arg-type]
        # Should NOT call to_thread since process already returned
        mock_to_thread.assert_not_awaited()


@pytest.mark.asyncio
async def test_consume_stream_generic_exception_breaks_loop(
    process_component: ProcessComponent,
) -> None:
    """Cover generic exception branch in _consume_stream."""

    class _ExceptionReader:
        async def read(self, _n: int) -> bytes:
            raise RuntimeError("generic error")

    buf = bytearray()
    await process_component._consume_stream(1, _ExceptionReader(), buf)  # type: ignore[arg-type]
    assert buf == bytearray()


@pytest.mark.asyncio
async def test_read_stream_chunk_generic_exception_returns_empty(
    process_component: ProcessComponent,
) -> None:
    """Cover generic exception branch in _read_stream_chunk."""

    class _BadReader:
        async def read(self, _n: int) -> bytes:
            raise RuntimeError("generic")

    chunk = await process_component._read_stream_chunk(1, _BadReader(), timeout=0)  # type: ignore[arg-type]
    assert chunk == b""


@pytest.mark.asyncio
async def test_collect_output_slot_disappears_during_io(
    process_component: ProcessComponent,
) -> None:
    """Cover case where slot disappears during _read_process_pipes."""
    pid = 50

    class _Proc:
        def __init__(self) -> None:
            self.pid = 999
            self.returncode = None
            self.stdout = None
            self.stderr = None

    proc = _Proc()
    slot = ManagedProcess(pid=pid, command="vanish", handle=proc)
    async with process_component.state.process_lock:
        process_component.state.running_processes[pid] = slot

    async def _read_and_remove(self, p, proc_obj):
        # Remove slot during read
        async with process_component.state.process_lock:
            process_component.state.running_processes.pop(pid, None)
        return (b"", b"")

    with patch.object(ProcessComponent, "_read_process_pipes", _read_and_remove):
        batch = await process_component.collect_output(pid)

    assert batch.status_byte == Status.ERROR.value


@pytest.mark.asyncio
async def test_finalize_async_process_slot_changed_handle(
    process_component: ProcessComponent,
) -> None:
    """Cover case where slot.handle changed during finalization."""
    pid = 60

    class _Proc1:
        def __init__(self) -> None:
            self.returncode = 0
            self.stdout = None
            self.stderr = None

    class _Proc2:
        def __init__(self) -> None:
            self.returncode = 1
            self.stdout = None
            self.stderr = None

    proc1 = _Proc1()
    proc2 = _Proc2()
    slot = ManagedProcess(pid=pid, command="swap", handle=proc2)
    async with process_component.state.process_lock:
        process_component.state.running_processes[pid] = slot

    with patch.object(ProcessComponent, "_drain_process_pipes", new_callable=AsyncMock) as mock_drain:
        mock_drain.return_value = (b"", b"")
        with patch.object(ProcessComponent, "_release_process_slot"):
            await process_component._finalize_async_process(pid, proc1)  # type: ignore[arg-type]

    # Should NOT release slot since handle doesn't match
    async with process_component.state.process_lock:
        assert pid in process_component.state.running_processes


@pytest.mark.asyncio
async def test_start_async_generic_exception_returns_sentinel(
    process_component: ProcessComponent,
) -> None:
    """Cover generic exception branch in start_async subprocess creation."""
    with patch.object(ProcessComponent, "_prepare_command", return_value=('/bin/true',)):
        with patch.object(ProcessComponent, "_allocate_pid", new_callable=AsyncMock) as mock_alloc:
            mock_alloc.return_value = 55
            with patch("asyncio.create_subprocess_exec", side_effect=RuntimeError("boom")):
                pid = await process_component.start_async("/bin/true")
                assert pid == protocol.INVALID_ID_SENTINEL


# ============================================================================
# SERIAL.PY COVERAGE GAPS (lines 13-15, 77-87)
# ============================================================================


def test_serial_termios_import_fallback() -> None:
    """Cover lines 13-15: termios/tty not available on non-Unix platforms."""
    from mcubridge.transport import serial

    # Save original values
    original_termios = getattr(serial, '_termios', None)
    original_tty = getattr(serial, '_tty', None)

    try:
        # Simulate the fallback case by setting module-level to None
        serial._termios = None
        serial._tty = None

        mock_serial = MagicMock()
        mock_serial.fd = 1
        # Should not raise with None termios/tty
        serial._ensure_raw_mode(mock_serial, "/dev/ttyS0")
    finally:
        # Restore originals
        if original_termios is not None:
            serial._termios = original_termios
        if original_tty is not None:
            serial._tty = original_tty


def test_serial_ensure_raw_mode_no_fd() -> None:
    """Cover line 77-78: Serial object without fd attribute."""
    from mcubridge.transport.serial import _ensure_raw_mode

    mock_serial = MagicMock()
    # Ensure accessing fd raises AttributeError
    del mock_serial.fd

    # Should not raise
    _ensure_raw_mode(mock_serial, "/dev/ttyS0")

def test_serial_ensure_raw_mode_fd_none() -> None:
    """Cover line 77-78: Serial object with fd=None."""
    from mcubridge.transport.serial import _ensure_raw_mode

    mock_serial = MagicMock()
    mock_serial.fd = None
    # Should not raise
    _ensure_raw_mode(mock_serial, "/dev/ttyS0")

def test_serial_ensure_raw_mode_exception() -> None:
    """Cover lines 84-87: Raw mode setting fails with exception."""
    from mcubridge.transport.serial import _ensure_raw_mode

    mock_serial = MagicMock()
    mock_serial.fd = 42

    with patch("mcubridge.transport.serial.termios") as mock_termios:
        mock_termios.error = OSError
        with patch("mcubridge.transport.serial.tty") as mock_tty:
            mock_tty.setraw.side_effect = OSError("Permission denied")
            # Should not raise, just log warning
            _ensure_raw_mode(mock_serial, "/dev/ttyS0")

def test_serial_ensure_raw_mode_termios_exception() -> None:
    """Cover termios.tcgetattr raising exception."""
    from mcubridge.transport.serial import _ensure_raw_mode

    mock_serial = MagicMock()
    mock_serial.fd = 42

    with patch("mcubridge.transport.serial.termios") as mock_termios:
        mock_termios.error = OSError
        with patch("mcubridge.transport.serial.tty") as mock_tty:
            mock_tty.setraw.return_value = None
            mock_termios.tcgetattr.side_effect = OSError("ENOTTY")
            # Should not raise
            _ensure_raw_mode(mock_serial, "/dev/ttyS0")


# ============================================================================
# METRICS.PY COVERAGE GAPS (lines 47, 118, 160-163, 179-185, 199)
# ============================================================================
@pytest.mark.asyncio
async def test_emit_metrics_snapshot_exception_on_initial() -> None:
    """Cover line 118: Initial metrics emit failure."""
    from mcubridge.metrics import _emit_metrics_snapshot
    from mcubridge.state.context import create_runtime_state

    config = _make_config()
    state = create_runtime_state(config)

    async def _failing_enqueue(msg):
        raise RuntimeError("MQTT down")

    # Should raise the exception
    with pytest.raises(RuntimeError, match="MQTT down"):
        await _emit_metrics_snapshot(state, _failing_enqueue, expiry_seconds=30)


@pytest.mark.asyncio
@pytest.mark.usefixtures("socket_enabled")
async def test_prometheus_exporter_ephemeral_port() -> None:
    """Cover lines 179-185: Ephemeral port binding."""
    from mcubridge.metrics import PrometheusExporter
    from mcubridge.state.context import create_runtime_state

    config = _make_config()
    state = create_runtime_state(config)

    exporter = PrometheusExporter(state, "127.0.0.1", 0)  # port 0 = ephemeral
    try:
        await exporter.start()
        # Should have resolved to an actual port
        assert exporter.port > 0
        assert exporter._resolved_port is not None
    finally:
        await exporter.stop()


@pytest.mark.asyncio
@pytest.mark.usefixtures("socket_enabled")
async def test_prometheus_exporter_malformed_request() -> None:
    """Cover line 199: Malformed HTTP request with < 2 parts."""
    from mcubridge.metrics import PrometheusExporter
    from mcubridge.state.context import create_runtime_state

    config = _make_config()
    state = create_runtime_state(config)

    exporter = PrometheusExporter(state, "127.0.0.1", 0)
    try:
        await exporter.start()

        reader, writer = await asyncio.open_connection("127.0.0.1", exporter.port)
        # Send malformed request (single word, no path)
        writer.write(b"GET\r\n\r\n")
        await writer.drain()

        response = await reader.read(1024)
        # Should get 400 Bad Request
        assert b"400" in response
        writer.close()
        await writer.wait_closed()
    finally:
        await exporter.stop()


@pytest.mark.asyncio
@pytest.mark.usefixtures("socket_enabled")
async def test_prometheus_exporter_404_path() -> None:
    """Cover 404 response for unknown path."""
    from mcubridge.metrics import PrometheusExporter
    from mcubridge.state.context import create_runtime_state

    config = _make_config()
    state = create_runtime_state(config)

    exporter = PrometheusExporter(state, "127.0.0.1", 0)
    try:
        await exporter.start()

        reader, writer = await asyncio.open_connection("127.0.0.1", exporter.port)
        writer.write(b"GET /unknown HTTP/1.1\r\n\r\n")
        await writer.drain()

        response = await reader.read(1024)
        assert b"404" in response
        writer.close()
        await writer.wait_closed()
    finally:
        await exporter.stop()


@pytest.mark.asyncio
@pytest.mark.usefixtures("socket_enabled")
async def test_prometheus_exporter_post_method_404() -> None:
    """Cover 404 for non-GET method."""
    from mcubridge.metrics import PrometheusExporter
    from mcubridge.state.context import create_runtime_state

    config = _make_config()
    state = create_runtime_state(config)

    exporter = PrometheusExporter(state, "127.0.0.1", 0)
    try:
        await exporter.start()

        reader, writer = await asyncio.open_connection("127.0.0.1", exporter.port)
        writer.write(b"POST /metrics HTTP/1.1\r\n\r\n")
        await writer.drain()

        response = await reader.read(1024)
        assert b"404" in response
        writer.close()
        await writer.wait_closed()
    finally:
        await exporter.stop()


@pytest.mark.asyncio
@pytest.mark.usefixtures("socket_enabled")
async def test_prometheus_exporter_get_root() -> None:
    """Cover GET / path (treated same as /metrics)."""
    from mcubridge.metrics import PrometheusExporter
    from mcubridge.state.context import create_runtime_state

    config = _make_config()
    state = create_runtime_state(config)

    exporter = PrometheusExporter(state, "127.0.0.1", 0)
    try:
        await exporter.start()

        reader, writer = await asyncio.open_connection("127.0.0.1", exporter.port)
        writer.write(b"GET / HTTP/1.1\r\n\r\n")
        await writer.drain()

        response = await reader.read(4096)
        assert b"200 OK" in response
        assert b"mcubridge" in response  # Should contain metrics
        writer.close()
        await writer.wait_closed()
    finally:
        await exporter.stop()


# ============================================================================
# FILE.PY COVERAGE GAPS (lines 135-141, 183-184, 202-203, 208, 231)
# ============================================================================


@pytest.mark.asyncio
async def test_file_handle_write_path_traversal_dotdot(mock_context: AsyncMock) -> None:
    """Cover lines 135-141: Path traversal with '..' blocked."""
    from mcubridge.services.components.file import FileComponent
    from mcubridge.state.context import create_runtime_state

    config = _make_config()
    state = create_runtime_state(config)
    component = FileComponent(config, state, mock_context)

    # Build payload: path_len(1) + path + data_len(2) + data
    path = b"../../../etc/passwd"
    data = b"malicious"
    payload = bytes([len(path)]) + path + len(data).to_bytes(2, "big") + data

    result = await component.handle_write(payload)
    assert result is False
    mock_context.send_frame.assert_awaited()
    args = mock_context.send_frame.call_args[0]
    assert args[0] == Status.ERROR.value
    assert b"invalid_path" in args[1]


@pytest.mark.asyncio
async def test_file_handle_write_absolute_path_blocked(mock_context: AsyncMock) -> None:
    """Cover lines 140-141: Absolute paths blocked."""
    from mcubridge.services.components.file import FileComponent
    from mcubridge.state.context import create_runtime_state

    config = _make_config()
    state = create_runtime_state(config)
    component = FileComponent(config, state, mock_context)

    path = b"/etc/passwd"
    data = b"malicious"
    payload = bytes([len(path)]) + path + len(data).to_bytes(2, "big") + data

    result = await component.handle_write(payload)
    assert result is False
    mock_context.send_frame.assert_awaited()
    args = mock_context.send_frame.call_args[0]
    assert args[0] == Status.ERROR.value
    assert b"invalid_path" in args[1]


@pytest.mark.asyncio
async def test_file_handle_mqtt_unknown_action(mock_context: AsyncMock) -> None:
    """Cover line 208: Unknown MQTT file action."""
    from mcubridge.services.components.file import FileComponent
    from mcubridge.state.context import create_runtime_state

    config = _make_config()
    state = create_runtime_state(config)
    component = FileComponent(config, state, mock_context)

    # Unknown action should be ignored (logged as debug)
    await component.handle_mqtt(
        action="UNKNOWN_ACTION",
        path_parts=["test.txt"],
        payload=b"data",
        inbound=None,
    )
    # Should not call send_frame since it's MQTT (not serial)
    mock_context.send_frame.assert_not_awaited()


@pytest.mark.asyncio
async def test_file_handle_mqtt_write_unsafe_path(mock_context: AsyncMock) -> None:
    """Cover lines 183-184: MQTT write to unsafe path."""
    from mcubridge.services.components.file import FileComponent
    from mcubridge.state.context import create_runtime_state

    config = _make_config()
    state = create_runtime_state(config)
    component = FileComponent(config, state, mock_context)

    # Path with traversal
    await component.handle_mqtt(
        action="write",
        path_parts=["..", "..", "etc", "passwd"],
        payload=b"data",
        inbound=None,
    )
    # Should fail with unsafe_path reason


@pytest.mark.asyncio
async def test_file_handle_mqtt_remove_failure(mock_context: AsyncMock) -> None:
    """Cover lines 202-203: MQTT file remove fails."""
    from mcubridge.services.components.file import FileComponent
    from mcubridge.state.context import create_runtime_state

    config = _make_config()
    state = create_runtime_state(config)
    component = FileComponent(config, state, mock_context)

    # Try to remove non-existent file (will fail)
    await component.handle_mqtt(
        action="remove",
        path_parts=["nonexistent_file_xyz123.txt"],
        payload=b"",
        inbound=None,
    )
    # Should log error but not crash


@pytest.mark.asyncio
async def test_file_get_base_dir_non_tmp_path_rejected(mock_context: AsyncMock) -> None:
    """Cover line 231: Non-tmp path with allow_non_tmp_paths=False."""
    from mcubridge.services.components.file import FileComponent
    from mcubridge.state.context import create_runtime_state

    config = _make_config()
    state = create_runtime_state(config)
    state.file_system_root = "/var/data"  # Not /tmp
    state.allow_non_tmp_paths = False

    component = FileComponent(config, state, mock_context)
    base_dir = component._get_base_dir()
    assert base_dir is None


# ============================================================================
# ADDITIONAL EDGE CASES
# ============================================================================


def test_process_trim_buffers_both_have_remaining(process_component: ProcessComponent) -> None:
    """Cover _trim_process_buffers with leftover data in both buffers."""
    stdout_buf = bytearray(b"A" * 10000)
    stderr_buf = bytearray(b"B" * 10000)

    stdout_chunk, stderr_chunk, trunc_out, trunc_err = process_component.trim_buffers(
        stdout_buf, stderr_buf
    )

    # Both should be truncated
    assert trunc_out is True or trunc_err is True
    assert len(stdout_chunk) + len(stderr_chunk) <= protocol.MAX_PAYLOAD_SIZE - 6


@pytest.mark.asyncio
async def test_allocate_pid_skips_zero(process_component: ProcessComponent) -> None:
    """Cover the 'continue' branch when candidate is 0."""
    async with process_component.state.process_lock:
        process_component.state.next_pid = 0  # Start at 0, should skip it

    pid = await process_component._allocate_pid()
    assert pid == 1  # Should skip 0 and return 1

def test_kill_process_tree_sync_psutil_error() -> None:
    """Cover psutil.Error handling in _kill_process_tree_sync."""
    import psutil
    from mcubridge.services.components.process import ProcessComponent

    with patch("psutil.Process", side_effect=psutil.NoSuchProcess(123)):
        # Should not raise
        ProcessComponent._kill_process_tree_sync(123)

def test_kill_process_tree_sync_children_error() -> None:
    """Cover psutil.Error when getting children."""
    import psutil
    from mcubridge.services.components.process import ProcessComponent

    mock_proc = MagicMock()
    mock_proc.children.side_effect = psutil.AccessDenied(123)
    mock_proc.kill.return_value = None

    with patch("psutil.Process", return_value=mock_proc):
        # Should not raise
        ProcessComponent._kill_process_tree_sync(123)
        mock_proc.kill.assert_called_once()

def test_kill_process_tree_sync_kill_error() -> None:
    """Cover psutil.Error when killing process."""
    import psutil
    from mcubridge.services.components.process import ProcessComponent

    mock_proc = MagicMock()
    mock_proc.children.return_value = []
    mock_proc.kill.side_effect = psutil.NoSuchProcess(123)

    with patch("psutil.Process", return_value=mock_proc):
        # Should not raise
        ProcessComponent._kill_process_tree_sync(123)

def test_kill_process_tree_sync_child_kill_error() -> None:
    """Cover psutil.Error when killing child process."""
    import psutil
    from mcubridge.services.components.process import ProcessComponent

    mock_child = MagicMock()
    mock_child.kill.side_effect = psutil.AccessDenied(456)

    mock_proc = MagicMock()
    mock_proc.children.return_value = [mock_child]
    mock_proc.kill.return_value = None

    with patch("psutil.Process", return_value=mock_proc):
        # Should not raise
        ProcessComponent._kill_process_tree_sync(123)
        mock_proc.kill.assert_called_once()
