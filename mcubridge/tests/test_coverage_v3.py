from typing import Any
import asyncio
import errno
import logging.handlers
import os
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import msgspec
import psutil
import pytest
from mcubridge import daemon
import logging
import mcubridge.config.logging
from mcubridge.config.settings import RuntimeConfig
from mcubridge.services.process import ProcessComponent
from mcubridge.transport.serial import (
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
        mcubridge.config.logging.configure_logging(config)

        root = logging.getLogger()
        assert len(root.handlers) == 1
        assert isinstance(root.handlers[0], logging.StreamHandler)
        assert not isinstance(
            root.handlers[0],
            logging.handlers.SysLogHandler,  # type: ignore[reportUnknownMemberType, reportAttributeAccessIssue]
        )


def test_configure_logging_syslog_fallback(tmp_path: Any):
    config = create_real_config()
    fake_fallback = tmp_path / "log_fallback"
    fake_fallback.touch()

    with (
        patch("mcubridge.config.logging.SYSLOG_SOCKET", Path("/non/existent/dev/log")),
        patch("mcubridge.config.logging.SYSLOG_SOCKET_FALLBACK", fake_fallback),
        patch("logging.handlers.SysLogHandler", autospec=True) as mock_cls,
    ):
        mcubridge.config.logging.configure_logging(config)
        mock_cls.assert_called_once_with(
            address=str(fake_fallback),
            facility=logging.handlers.SysLogHandler.LOG_DAEMON,
        )


def test_configure_logging_debug():
    config = create_real_config()
    config.debug_logging = True
    mcubridge.config.logging.configure_logging(config)

    root = logging.getLogger()
    assert root.level == logging.DEBUG


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
        daemon._cleanup_child_processes()  # type: ignore[reportPrivateUsage]
        mock_zombie.kill.assert_called_once()


@pytest.mark.asyncio
async def test_supervise_task_retry_error():
    from types import SimpleNamespace

    spec = SimpleNamespace(
        name="test-task",
        factory=AsyncMock(side_effect=RuntimeError("Fail")),
        fatal_exceptions=(),
        max_restarts=0,
        min_backoff=0.01,
        max_backoff=0.02,
    )
    d = daemon.BridgeDaemon(create_real_config())
    try:
        with pytest.raises(RuntimeError):
            await d._supervise(  # type: ignore[reportPrivateUsage]
                spec.name,
                spec.factory,
                spec.fatal_exceptions,
                max_restarts=spec.max_restarts,
                min_backoff=spec.min_backoff,
                max_backoff=spec.max_backoff,
            )
    finally:
        d.state.cleanup()


@pytest.mark.asyncio
async def test_supervise_task_telemetry_error_path():
    from types import SimpleNamespace

    spec = SimpleNamespace(
        name="test-task",
        factory=AsyncMock(side_effect=RuntimeError("Fail")),
        fatal_exceptions=(),
        max_restarts=0,
        min_backoff=0.01,
        max_backoff=0.02,
    )
    d = daemon.BridgeDaemon(create_real_config())

    mock_retryer = AsyncMock()
    mock_retryer.statistics = MagicMock()
    type(mock_retryer.statistics).get = MagicMock(side_effect=TypeError("invalid"))

    async def fake_iter(*args: Any, **kwargs: Any):
        yield MagicMock()

    mock_retryer.__aiter__ = fake_iter

    try:
        with (
            patch("tenacity.AsyncRetrying", return_value=mock_retryer),
            pytest.raises(RuntimeError),
        ):
            await d._supervise(  # type: ignore[reportPrivateUsage]
                spec.name,
                spec.factory,
                spec.fatal_exceptions,
                max_restarts=spec.max_restarts,
                min_backoff=spec.min_backoff,
                max_backoff=spec.max_backoff,
            )
    finally:
        d.state.cleanup()


@pytest.mark.asyncio
async def test_daemon_run_exception_group_coverage():
    config = create_real_config()
    d = daemon.BridgeDaemon(config)
    try:

        class FakeTaskGroup:
            async def __aenter__(self):
                return self

            async def __aexit__(self: Any, exc_type: Any, exc_val: Any, exc_tb: Any):
                raise ExceptionGroup("Main Group", [RuntimeError("Sub-error")])

            def create_task(self: Any, coro: Any):
                coro.close()
                return MagicMock(spec=asyncio.Task)

        with (
            patch("asyncio.TaskGroup", return_value=FakeTaskGroup()),
            patch.object(d.service, "__aenter__", new_callable=AsyncMock),
            patch.object(d.service, "__aexit__", new_callable=AsyncMock),
            patch("mcubridge.daemon._cleanup_child_processes"),
            patch("pathlib.Path.unlink"),
            pytest.raises(ExceptionGroup),
        ):
            await d.run()
    finally:
        d.state.cleanup()


@pytest.mark.asyncio
async def test_cleanup_child_processes_alive():
    mock_child = MagicMock()
    mock_child.terminate.side_effect = None

    with (
        patch("psutil.Process") as mock_proc_cls,
        patch("psutil.wait_procs", return_value=([], [mock_child])),  # Still alive
    ):
        mock_proc_cls.return_value.children.return_value = [mock_child]
        daemon._cleanup_child_processes()  # type: ignore[reportPrivateUsage]
        mock_child.kill.assert_called_once()


# --- mcubridge.services.process ---


@pytest.mark.asyncio
async def test_process_run_async_limit_reached():
    config = create_real_config()
    config.process_max_concurrent = 1
    state = MagicMock()
    state.process_max_concurrent = 1
    state.process_lock = asyncio.Lock()
    ctx = MagicMock()

    comp = ProcessComponent(config, state, ctx)
    await comp._process_slots.acquire()  # type: ignore[reportPrivateUsage]

    try:
        async with asyncio.timeout(0.1):
            pid = await comp.run_async("ls")
            assert pid == 0
    except asyncio.TimeoutError:
        pass


@pytest.mark.asyncio
async def test_process_run_async_os_error():
    config = create_real_config()
    state = MagicMock()
    state.process_max_concurrent = 2
    state.process_lock = asyncio.Lock()
    # Initialize state attributes to avoid MagicMock returns
    state.next_pid = 1
    ctx = MagicMock()
    comp = ProcessComponent(config, state, ctx)

    with patch("asyncio.create_subprocess_shell", side_effect=OSError("Not found")):
        pid = await comp.run_async("cmd")
        assert pid == 0


@pytest.mark.asyncio
async def test_process_stop_process_not_found():
    config = create_real_config()
    state = MagicMock()
    state.process_lock = asyncio.Lock()
    state.running_processes = {}
    comp = ProcessComponent(config, state, MagicMock())

    success = await comp.stop_process(999)
    assert success is False


@pytest.mark.asyncio
async def test_process_finalize_process_missing_slot():
    config = create_real_config()
    state = MagicMock()
    state.process_lock = asyncio.Lock()
    state.running_processes = {}
    comp = ProcessComponent(config, state, MagicMock())

    # Should not raise
    await comp._finalize_process(999)  # type: ignore[reportPrivateUsage]


# --- mcubridge.transport.serial ---


@pytest.mark.asyncio
async def test_serial_transport_toggle_dtr_error():
    config = create_real_config()
    state = MagicMock()
    service = MagicMock()
    transport = SerialTransport(config, state, service)

    with patch("serial.Serial", side_effect=OSError(errno.ENOTTY, "Not a typewriter")):
        await transport._toggle_dtr(asyncio.get_running_loop())  # type: ignore[reportPrivateUsage]


@pytest.mark.asyncio
async def test_serial_transport_run_fatal():
    config = create_real_config()
    config.reconnect_delay = 0.01  # type: ignore[reportAttributeAccessIssue]
    state = MagicMock()
    service = MagicMock()
    transport = SerialTransport(config, state, service)

    from mcubridge.services.handshake import SerialHandshakeFatal

    with patch.object(
        transport, "_retryable_run", side_effect=SerialHandshakeFatal("Fatal")
    ):
        with pytest.raises(SerialHandshakeFatal):
            await transport.run()


@pytest.mark.asyncio
async def test_serial_transport_on_disconnected_hook_error():
    config = create_real_config()
    state = MagicMock()
    service = MagicMock()
    service.on_serial_connected = AsyncMock()
    service.on_serial_disconnected = AsyncMock(side_effect=RuntimeError("Hook fail"))
    transport = SerialTransport(config, state, service)

    orig_run = SerialTransport._retryable_run.__wrapped__  # type: ignore[reportPrivateUsage]
    with (
        patch.object(transport, "_toggle_dtr", new_callable=AsyncMock),
        patch(
            "mcubridge.transport.serial.serial_asyncio_fast.open_serial_connection",
            side_effect=OSError("Connect fail"),
        ),
    ):
        with pytest.raises(OSError):
            await orig_run(transport, asyncio.get_running_loop())


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
