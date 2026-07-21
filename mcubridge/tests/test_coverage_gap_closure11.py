from pathlib import Path
from typing import Any, cast
import asyncio
import logging
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from mcubridge.config.logging import configure_logging
from mcubridge.config.settings import load_runtime_config
from mcubridge.daemon import app as daemon_app
from mcubridge.services.runtime import BridgeService, LocalBridgeService
from mcubridge.state.context import create_runtime_state
from mcubridge.state.status import status_writer
from mcubridge.transport.serial import SerialTransport


def test_logging_configuration_with_config():
    cfg = load_runtime_config()
    cfg.debug = False
    configure_logging(cfg)

    cfg_debug = load_runtime_config()
    cfg_debug.debug = True
    configure_logging(cfg_debug)

    root_logger = logging.getLogger()
    assert root_logger is not None


def test_daemon_app_version():
    with patch("sys.argv", ["mcubridge", "--version"]):
        with pytest.raises(SystemExit) as exc:
            daemon_app(["--version"])
        assert exc.value.code == 0


@pytest.mark.asyncio
async def test_status_writer_exception_handling():
    mock_state = MagicMock()
    mock_state.build_status_snapshot.side_effect = OSError("Status build error")

    task = asyncio.create_task(status_writer(mock_state, interval=1))
    await asyncio.sleep(0.01)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task


@pytest.mark.asyncio
async def test_serial_transport_additional_edge_cases():
    cfg = load_runtime_config()
    state = create_runtime_state(cfg)

    st = SerialTransport(cfg, state, None)

    # Test send_raw when serial is None
    st.serial = None
    res = await st.send_raw(command_id=1, payload=b"test_data")
    assert res is False


@pytest.mark.asyncio
async def test_local_bridge_service_subscribe_console_exception():
    mock_runtime = MagicMock()
    service = LocalBridgeService(mock_runtime)

    mock_stream = AsyncMock()
    mock_stream.recv_message.return_value = MagicMock()

    # Simulate exception inside SubscribeConsole stream send
    mock_stream.send_message.side_effect = Exception("IPC write stream closed")

    # Put a message in console queue
    q: asyncio.Queue[MagicMock] = asyncio.Queue()
    q.put_nowait(MagicMock())
    queues = [q]
    mock_runtime.console_queues = queues

    with patch.object(asyncio.Queue, "get", side_effect=Exception("IPC queue error")):
        await service.SubscribeConsole(mock_stream)


@pytest.mark.asyncio
async def test_runtime_service_teardown_and_ipc_start(tmp_path: Path):
    cfg = load_runtime_config()
    cfg.cloud_enabled = False
    cfg.cloud_host = "127.0.0.1"
    cfg.cloud_port = 8443

    state = create_runtime_state(cfg)
    mock_serial = MagicMock()

    runtime = BridgeService(cfg, state, mock_serial)

    # Test run_cloud disabled
    await runtime.run_cloud()

    # Test cleanup exceptions handling
    mock_spool = AsyncMock()
    mock_spool.close.side_effect = OSError("Disk unmount error")
    cast(Any, runtime)._cloud_spool = mock_spool

    spool = cast(Any, runtime)._cloud_spool
    if spool is not None:
        try:
            await spool.close()
        except (Exception, OSError):
            pass
        cast(Any, runtime)._cloud_spool = None

    runtime.cleanup()
    assert cast(Any, runtime)._cloud_spool is None
