import asyncio
import errno
import os
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import msgspec
import psutil
import pytest
from cobs import cobs
from mcubridge import daemon
from mcubridge.config import logging as logging_config
from mcubridge.config.settings import RuntimeConfig
from mcubridge.protocol import protocol
from mcubridge.protocol.protocol import Command, Status
from mcubridge.services.process import ProcessComponent
from mcubridge.transport.serial import (
    BridgeSerialProtocol,
    SerialTransport,
)


def create_real_config():
    from mcubridge.config.common import get_default_config

    raw_cfg = get_default_config()
    raw_cfg.update(
        {
            "serial_port": "/dev/ttyFake",
            "serial_shared_secret": b"valid_secret_1234",
            "mqtt_spool_dir": "/tmp/spool_v3",
        }
    )
    return msgspec.convert(raw_cfg, RuntimeConfig)


# --- mcubridge.config.logging ---


def test_configure_logging_stream_env():
    config = create_real_config()
    with patch.dict(os.environ, {"MCUBRIDGE_LOG_STREAM": "1"}):
        with patch("mcubridge.config.logging.dictConfig") as mock_dict_config:
            logging_config.configure_logging(config)
            mock_dict_config.assert_called_once()
            assert mock_dict_config.call_args[0][0]["root"]["handlers"] == ["console"]


def test_configure_logging_syslog_fallback(tmp_path):
    config = create_real_config()
    fake_fallback = tmp_path / "log_fallback"
    fake_fallback.touch()

    original_exists = Path.exists

    def fake_exists(self):
        if str(self) == str(fake_fallback):
            return True
        if "/non/existent" in str(self):
            return False
        return original_exists(self)

    with (
        patch.object(Path, "exists", fake_exists),
        patch("mcubridge.config.logging.SYSLOG_SOCKET", Path("/non/existent/dev/log")),
        patch("mcubridge.config.logging.SYSLOG_SOCKET_FALLBACK", fake_fallback),
    ):
        with patch("mcubridge.config.logging.dictConfig") as mock_dict_config:
            logging_config.configure_logging(config)
            mock_dict_config.assert_called_once()
            handlers = mock_dict_config.call_args[0][0]["root"]["handlers"]
            assert "syslog" in handlers


def test_configure_logging_debug():
    config = create_real_config()
    config.debug_logging = True
    with patch("mcubridge.config.logging.dictConfig") as mock_dict_config:
        logging_config.configure_logging(config)
        mock_dict_config.assert_called_once()
        assert mock_dict_config.call_args[0][0]["root"]["level"] == "DEBUG"


# --- mcubridge.daemon ---


@pytest.mark.asyncio
async def test_cleanup_child_processes_coverage():
    mock_child = MagicMock()
    mock_child.terminate.side_effect = psutil.NoSuchProcess(123)

    mock_zombie = MagicMock()
    mock_zombie.pid = 456

    with (
        patch("psutil.Process") as mock_proc_cls,
        patch("psutil.wait_procs", return_value=([], [mock_zombie])),
    ):
        mock_proc_cls.return_value.children.return_value = [mock_child, mock_zombie]
        daemon._cleanup_child_processes()
        mock_zombie.kill.assert_called_once()


@pytest.mark.asyncio
async def test_supervise_task_retry_error():
    spec = daemon.SupervisedTaskSpec(
        name="test-task",
        factory=AsyncMock(side_effect=RuntimeError("Fail")),
        max_restarts=0,
        min_backoff=0.01,
        max_backoff=0.02,
    )
    d = daemon.BridgeDaemon(create_real_config())

    with pytest.raises(RuntimeError):
        await d._supervise(
            spec.name,
            spec.factory,
            spec.fatal_exceptions,
            max_restarts=spec.max_restarts,
            min_backoff=spec.min_backoff,
            max_backoff=spec.max_backoff,
        )


@pytest.mark.asyncio
async def test_supervise_task_telemetry_error_path():
    spec = daemon.SupervisedTaskSpec(
        name="test-task",
        factory=AsyncMock(side_effect=RuntimeError("Fail")),
        max_restarts=0,
    )
    d = daemon.BridgeDaemon(create_real_config())

    mock_retryer = AsyncMock()
    mock_retryer.statistics = MagicMock()
    type(mock_retryer.statistics).get = MagicMock(side_effect=TypeError("invalid"))

    async def fake_iter(*args, **kwargs):
        yield MagicMock()

    mock_retryer.__aiter__ = fake_iter

    with (
        patch("tenacity.AsyncRetrying", return_value=mock_retryer),
        pytest.raises(RuntimeError),
    ):
        await d._supervise(
            spec.name,
            spec.factory,
            spec.fatal_exceptions,
            max_restarts=spec.max_restarts,
            min_backoff=spec.min_backoff,
            max_backoff=spec.max_backoff,
        )


@pytest.mark.asyncio
async def test_daemon_run_exception_group_coverage():
    config = create_real_config()
    d = daemon.BridgeDaemon(config)

    class FakeTaskGroup:
        async def __aenter__(self):
            return self

        async def __aexit__(self, exc_type, exc_val, exc_tb):
            raise ExceptionGroup("Main Group", [RuntimeError("Sub-error")])

        def create_task(self, coro):
            # [FIX] Avoid RuntimeWarning by immediately closing the coroutine
            coro.close()
            return MagicMock(spec=asyncio.Task)

    with (
        patch("asyncio.TaskGroup", return_value=FakeTaskGroup()),
        patch.object(d.service, "__aenter__", new_callable=AsyncMock),
        patch.object(d.service, "__aexit__", new_callable=AsyncMock),
        patch("mcubridge.daemon._cleanup_child_processes"),
        patch("mcubridge.daemon.cleanup_status_file"),
        pytest.raises(ExceptionGroup),
    ):
        await d.run()


@pytest.mark.asyncio
async def test_cleanup_child_processes_alive():
    mock_child = MagicMock()
    mock_child.terminate.side_effect = None

    with (
        patch("psutil.Process") as mock_proc_cls,
        patch("psutil.wait_procs", return_value=([], [mock_child])),  # Still alive
    ):
        mock_proc_cls.return_value.children.return_value = [mock_child]
        daemon._cleanup_child_processes()
        mock_child.kill.assert_called_once()


# --- mcubridge.services.process ---


@pytest.mark.asyncio
async def test_process_handle_run_limit_reached():
    config = create_real_config()
    config.process_max_concurrent = 1
    state = MagicMock()
    state.allowed_policy.is_allowed.return_value = True
    ctx = MagicMock()
    ctx.send_frame = AsyncMock()

    comp = ProcessComponent(config, state, ctx)
    await comp._process_slots.acquire()

    from mcubridge.protocol.structures import ProcessRunPacket

    payload = ProcessRunPacket(command="ls").encode()

    await comp.handle_run(payload)
    ctx.send_frame.assert_called_once()
    assert ctx.send_frame.call_args[0][0] == Status.ERROR.value


@pytest.mark.asyncio
async def test_process_run_sync_os_error():
    config = create_real_config()
    state = MagicMock()
    ctx = MagicMock()
    comp = ProcessComponent(config, state, ctx)

    with patch("asyncio.create_subprocess_exec", side_effect=OSError("Not found")):
        status, stdout, stderr, exit_code = await comp.run_sync("cmd", ["cmd"])
        assert status == Status.ERROR.value
        assert b"Not found" in stderr


@pytest.mark.asyncio
async def test_process_kill_wait_timeout():
    config = create_real_config()
    state = MagicMock()
    state.process_lock = asyncio.Lock()
    ctx = MagicMock()
    ctx.send_frame = AsyncMock()

    comp = ProcessComponent(config, state, ctx)

    mock_proc = MagicMock()
    mock_proc.wait = AsyncMock(side_effect=asyncio.TimeoutError)
    mock_proc.returncode = None

    slot = MagicMock()
    slot.handle = mock_proc
    state.running_processes = {123: slot}

    from mcubridge.protocol.structures import ProcessKillPacket

    payload = ProcessKillPacket(pid=123).encode()

    with patch.object(comp, "_terminate_process_tree", new_callable=AsyncMock):
        await comp.handle_kill(payload)

    ctx.send_frame.assert_called()


@pytest.mark.asyncio
async def test_process_handle_run_async_validation_error():
    config = create_real_config()
    state = MagicMock()
    state.allowed_policy.is_allowed.return_value = False
    ctx = MagicMock()
    ctx.send_frame = AsyncMock()
    ctx.publish = AsyncMock()

    comp = ProcessComponent(config, state, ctx)
    from mcubridge.protocol.structures import ProcessRunAsyncPacket

    payload = ProcessRunAsyncPacket(command="forbidden").encode()

    await comp.handle_run_async(payload)
    ctx.send_frame.assert_called_once()
    assert ctx.send_frame.call_args[0][0] == Status.ERROR.value


@pytest.mark.asyncio
async def test_process_run_sync_exception_group():
    config = create_real_config()
    state = MagicMock()
    state.process_timeout = 5
    ctx = MagicMock()
    comp = ProcessComponent(config, state, ctx)

    mock_proc = MagicMock()
    mock_proc.stdout = AsyncMock()
    mock_proc.stderr = AsyncMock()
    mock_proc.wait = AsyncMock()

    with (
        patch("asyncio.create_subprocess_exec", return_value=mock_proc),
        patch(
            "asyncio.TaskGroup.__aenter__",
            side_effect=BaseExceptionGroup("Group", [OSError("Fail")]),
        ),
    ):
        status, stdout, stderr, exit_code = await comp.run_sync("cmd", ["cmd"])
        assert status == Status.ERROR.value


@pytest.mark.asyncio
async def test_process_handle_poll_unknown_pid():
    config = create_real_config()
    state = MagicMock()
    state.process_lock = asyncio.Lock()
    state.running_processes = {}
    ctx = MagicMock()
    ctx.send_frame = AsyncMock()
    ctx.publish = AsyncMock()
    comp = ProcessComponent(config, state, ctx)

    from mcubridge.protocol.structures import ProcessPollPacket

    payload = ProcessPollPacket(pid=999).encode()
    await comp.handle_poll(payload)
    ctx.send_frame.assert_called_once()


@pytest.mark.asyncio
async def test_process_handle_kill_non_existent():
    config = create_real_config()
    state = MagicMock()
    state.process_lock = asyncio.Lock()
    state.running_processes = {}
    ctx = MagicMock()
    ctx.send_frame = AsyncMock()
    comp = ProcessComponent(config, state, ctx)

    from mcubridge.protocol.structures import ProcessKillPacket

    payload = ProcessKillPacket(pid=999).encode()
    await comp.handle_kill(payload)
    ctx.send_frame.assert_called_once()


@pytest.mark.asyncio
async def test_process_run_sync_timeout_coverage():
    config = create_real_config()
    state = MagicMock()
    state.process_timeout = 0.1
    state.process_output_limit = 1024
    ctx = MagicMock()
    comp = ProcessComponent(config, state, ctx)

    mock_proc = MagicMock()
    mock_proc.wait = AsyncMock(side_effect=asyncio.TimeoutError)
    mock_proc.returncode = -1

    with (
        patch("asyncio.create_subprocess_exec", return_value=mock_proc),
        patch.object(comp, "_consume_stream", new_callable=AsyncMock),
        patch.object(comp, "_terminate_process_tree", new_callable=AsyncMock),
    ):
        status, stdout, stderr, exit_code = await comp.run_sync("sleep 10", ["sleep", "10"])
        assert status == Status.TIMEOUT.value


@pytest.mark.asyncio
async def test_process_allocate_pid_exhaustion():
    config = create_real_config()
    state = MagicMock()
    state.process_lock = asyncio.Lock()
    state.next_pid = 1
    state.running_processes = {i: True for i in range(1, 65536)}

    comp = ProcessComponent(config, state, MagicMock())
    pid = await comp._allocate_pid()
    assert pid == protocol.INVALID_ID_SENTINEL


def test_process_trim_buffers_coverage():
    comp = ProcessComponent(create_real_config(), MagicMock(), MagicMock())
    stdout = bytearray(b"A" * 100)
    stderr = bytearray(b"B" * 100)
    out, err, t_out, t_err = comp.trim_buffers(stdout, stderr)
    assert len(out) + len(err) <= 58
    assert t_out is True
    assert t_err is True


@pytest.mark.asyncio
async def test_process_start_async_os_error():
    config = create_real_config()
    state = MagicMock()
    state.process_lock = asyncio.Lock()
    ctx = MagicMock()
    comp = ProcessComponent(config, state, ctx)

    with (
        patch.object(comp, "_allocate_pid", return_value=123),
        patch("asyncio.create_subprocess_exec", side_effect=OSError("Failed")),
    ):
        pid = await comp.start_async("ls", ["ls"])
        assert pid == protocol.INVALID_ID_SENTINEL


@pytest.mark.asyncio
async def test_process_consume_stream_error():
    config = create_real_config()
    state = MagicMock()
    comp = ProcessComponent(config, state, MagicMock())

    mock_reader = AsyncMock()
    mock_reader.read.side_effect = RuntimeError("IO Error")
    buffer = bytearray()
    await comp._consume_stream(123, mock_reader, buffer)
    assert len(buffer) == 0


@pytest.mark.asyncio
async def test_process_terminate_process_tree_no_pid():
    config = create_real_config()
    state = MagicMock()
    comp = ProcessComponent(config, state, MagicMock())

    mock_proc = MagicMock()
    mock_proc.returncode = None
    mock_proc.pid = None

    await comp._terminate_process_tree(mock_proc)
    mock_proc.kill.assert_called_once()


@pytest.mark.asyncio
async def test_process_finalize_async_process_slot_missing():
    config = create_real_config()
    state = MagicMock()
    state.process_lock = asyncio.Lock()
    state.running_processes = {}  # Empty
    comp = ProcessComponent(config, state, MagicMock())

    mock_proc = MagicMock()
    with patch.object(comp, "_release_process_slot") as mock_release:
        await comp._finalize_async_process(123, mock_proc)
        mock_release.assert_called_once()


def test_process_limit_sync_payload_truncation():
    comp = ProcessComponent(create_real_config(), MagicMock(), MagicMock())
    comp.state.process_output_limit = 10
    payload = b"A" * 20
    res, truncated = comp._limit_sync_payload(payload)
    assert len(res) == 10
    assert truncated is True
    assert res == b"A" * 10


@pytest.mark.asyncio
async def test_process_read_stream_chunk_timeout():
    comp = ProcessComponent(create_real_config(), MagicMock(), MagicMock())
    mock_reader = AsyncMock()
    mock_reader.read.side_effect = asyncio.TimeoutError()
    res = await comp._read_stream_chunk(123, mock_reader, timeout=0.01)
    assert res == b""


@pytest.mark.asyncio
async def test_process_kill_process_tree_sync_psutil_error():
    with patch("psutil.Process", side_effect=psutil.NoSuchProcess(123)):
        ProcessComponent._kill_process_tree_sync(123)


@pytest.mark.asyncio
async def test_process_run_sync_wait_timeout_kill_timeout():
    config = create_real_config()
    state = MagicMock()
    state.process_timeout = 0.1
    state.process_output_limit = 1024
    comp = ProcessComponent(config, state, MagicMock())

    mock_proc = MagicMock()
    mock_proc.wait = AsyncMock(side_effect=asyncio.TimeoutError)
    mock_proc.returncode = -1

    with (
        patch("asyncio.create_subprocess_exec", return_value=mock_proc),
        patch.object(comp, "_consume_stream", new_callable=AsyncMock),
        patch.object(comp, "_terminate_process_tree", new_callable=AsyncMock),
    ):
        await comp.run_sync("sleep 10", ["sleep", "10"])
        assert mock_proc.wait.call_count >= 2

    # --- mcubridge.transport.serial ---

    retry_state = MagicMock()
    retry_state.attempt_number = 2


@pytest.mark.asyncio
async def test_serial_protocol_connection_lost_branches():
    proto = BridgeSerialProtocol(MagicMock(), MagicMock(), asyncio.get_running_loop())
    proto.connection_lost(RuntimeError("Lost"))
    assert proto.connected_future.done()
    with pytest.raises(RuntimeError):
        proto.connected_future.result()

    proto.connected_future = asyncio.get_running_loop().create_future()
    proto.connected_future.set_result(None)
    proto.connection_lost(None)


@pytest.mark.asyncio
async def test_serial_protocol_data_received_discarding():
    proto = BridgeSerialProtocol(MagicMock(), MagicMock(), asyncio.get_running_loop())
    proto._discarding = True
    proto.data_received(b"some data\x00")
    assert proto._discarding is False
    assert len(proto._buffer) == 0


@pytest.mark.asyncio
async def test_serial_transport_toggle_dtr_error():
    config = create_real_config()
    state = MagicMock()
    service = MagicMock()
    transport = SerialTransport(config, state, service)

    with patch("serial.Serial", side_effect=OSError(errno.ENOTTY, "Not a typewriter")):
        await transport._toggle_dtr(asyncio.get_event_loop())


@pytest.mark.asyncio
async def test_serial_transport_run_fatal():
    config = create_real_config()
    config.reconnect_delay = 0.01
    state = MagicMock()
    service = MagicMock()
    transport = SerialTransport(config, state, service)

    from mcubridge.services.handshake import SerialHandshakeFatal

    with patch.object(transport, "_connect_and_run", side_effect=SerialHandshakeFatal("Fatal")):
        with pytest.raises(SerialHandshakeFatal):
            await transport.run()


@pytest.mark.asyncio
async def test_serial_transport_negotiate_baudrate_write_fail():
    config = create_real_config()
    state = MagicMock()
    service = MagicMock()
    transport = SerialTransport(config, state, service)
    mock_proto = MagicMock()
    mock_proto.write_frame.return_value = False
    mock_proto.loop = asyncio.get_running_loop()

    res = await transport._negotiate_baudrate(mock_proto, 115200)
    assert res is False


@pytest.mark.asyncio
async def test_serial_transport_on_disconnected_hook_error():
    config = create_real_config()
    state = MagicMock()
    service = MagicMock()
    service.on_serial_disconnected = AsyncMock(side_effect=RuntimeError("Hook fail"))
    transport = SerialTransport(config, state, service)

    with (
        patch.object(transport, "_toggle_dtr", new_callable=AsyncMock),
        patch(
            "mcubridge.transport.serial.serial_asyncio_fast.create_serial_connection",
            side_effect=OSError("Connect fail"),
        ),
    ):
        with pytest.raises(OSError):
            await transport._connect_and_run(asyncio.get_running_loop())


@pytest.mark.asyncio
async def test_serial_protocol_negotiation_logic():
    proto = BridgeSerialProtocol(MagicMock(), MagicMock(), asyncio.get_running_loop())
    proto.negotiation_future = asyncio.get_running_loop().create_future()

    from mcubridge.protocol.frame import Frame

    raw_frame = Frame.build(75, b"")
    encoded = cobs.encode(raw_frame)

    proto._process_packet(encoded)
    assert proto.negotiation_future.result() is True


@pytest.mark.asyncio
async def test_serial_protocol_async_process_compressed():
    service = MagicMock()
    service.handle_mcu_frame = AsyncMock()
    proto = BridgeSerialProtocol(service, MagicMock(), asyncio.get_running_loop())

    from mcubridge.protocol import rle
    from mcubridge.protocol.frame import Frame

    payload = b"A" * 10
    compressed = rle.encode(payload)
    cmd = Command.CMD_CONSOLE_WRITE.value | 0x8000
    raw_frame = Frame.build(cmd, compressed)
    encoded = cobs.encode(raw_frame)

    await proto._async_process_packet(encoded)
    service.handle_mcu_frame.assert_called_once()
    assert service.handle_mcu_frame.call_args[0][1] == payload


@pytest.mark.asyncio
async def test_serial_protocol_write_frame_fail():
    proto = BridgeSerialProtocol(MagicMock(), MagicMock(), asyncio.get_running_loop())
    proto.transport = MagicMock()
    proto.transport.is_closing.return_value = False

    with patch("mcubridge.protocol.frame.Frame.build", side_effect=ValueError("Boom")):
        assert proto.write_frame(1, b"") is False


# --- mcubridge.config.settings ---


def test_runtime_config_post_init_errors():
    from mcubridge.config.settings import RuntimeConfig

    with pytest.raises(ValueError, match="watchdog_interval must be >= 0.5s"):
        RuntimeConfig(
            serial_port="/dev/ttyS0",
            serial_shared_secret=b"valid_secret_1234",
            watchdog_interval=0.1,
        )

    with pytest.raises(ValueError, match="serial_response_timeout must be at least 2x"):
        RuntimeConfig(
            serial_port="/dev/ttyS0",
            serial_shared_secret=b"valid_secret_1234",
            serial_retry_timeout=5.0,
            serial_response_timeout=1.0,
        )
