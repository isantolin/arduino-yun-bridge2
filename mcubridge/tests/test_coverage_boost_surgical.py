# pyright: reportPrivateUsage=false
"""Surgical unit tests boosting coverage across runtime.py, serial.py, and pin_rest_cgi.py."""

from __future__ import annotations
from mcubridge.transport.serial import SerialTransport
from mcubridge.services.runtime import BridgeService, LocalBridgeService
from mcubridge.protocol import mcubridge_pb2 as pb
from mcubridge.config.settings import RuntimeConfig

import asyncio
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

import importlib.util
import sys
import types
from pathlib import Path

# Ensure 'uci' mock exists before importing pin_rest_cgi
if "uci" not in sys.modules:
    sys.modules["uci"] = types.ModuleType("uci")

if "pin_rest_cgi" in sys.modules:
    pin_rest_cgi = sys.modules["pin_rest_cgi"]
else:
    script_path = Path(__file__).parent.parent / "scripts" / "pin_rest_cgi.py"
    spec = importlib.util.spec_from_file_location("pin_rest_cgi", str(script_path))
    pin_rest_cgi = importlib.util.module_from_spec(spec)  # type: ignore
    sys.modules["pin_rest_cgi"] = pin_rest_cgi
    spec.loader.exec_module(pin_rest_cgi)  # type: ignore


def test_pin_rest_cgi_publish_sync_error() -> None:
    config = RuntimeConfig(allowed_commands=("echo",), serial_shared_secret=b"testsecret")
    with patch("grpclib.client.Channel", side_effect=OSError("IPC Connection Failed")):
        with pytest.raises(OSError):
            pin_rest_cgi.publish_sync("br/d/13/write", "1", config)


def test_pin_rest_cgi_application() -> None:
    from io import BytesIO

    start_response = MagicMock()
    body = b'{"state": "ON"}'
    env = {
        "PATH_INFO": "/pin/13",
        "REQUEST_METHOD": "POST",
        "CONTENT_LENGTH": str(len(body)),
        "wsgi.input": BytesIO(body),
    }

    with patch.object(pin_rest_cgi, "publish_sync") as mock_pub:
        res = pin_rest_cgi.application(env, start_response)
        assert res
        start_response.assert_called_once()
        mock_pub.assert_called_once()

    start_response_err = MagicMock()
    env_invalid = {"PATH_INFO": "/invalid", "REQUEST_METHOD": "GET"}
    res_err = pin_rest_cgi.application(env_invalid, start_response_err)
    assert res_err
    start_response_err.assert_called_with(
        "400 Bad Request", [("Content-Type", "application/json"), ("Content-Length", "52")]
    )


@pytest.mark.asyncio
async def test_local_bridge_service_ipc() -> None:
    from mcubridge.state.context import create_runtime_state
    import time
    import os

    fs_root = f".tmp_tests/mcubridge-test-fs-{os.getpid()}-{time.time_ns()}"
    spool_dir = f".tmp_tests/mcubridge-test-spool-{os.getpid()}-{time.time_ns()}"
    config = RuntimeConfig(
        allowed_commands=("echo", "ls"),
        serial_shared_secret=b"testshared",
        file_system_root=fs_root,
        cloud_spool_dir=spool_dir,
        allow_non_tmp_paths=True,
    )
    state = create_runtime_state(config)
    try:
        mock_serial = AsyncMock(spec=SerialTransport)
        svc = BridgeService(config, state, mock_serial)
        local_svc = LocalBridgeService(svc)

        # Test Publish with no stream message
        mock_stream = AsyncMock()
        mock_stream.recv_message.return_value = None
        await local_svc.Publish(mock_stream)
        mock_stream.send_message.assert_not_called()

        # Test Publish with message & correlation
        req_msg = pb.CloudQueuedPublish(topic_name="br/d/13/read", correlation_data=b"123456789012")
        mock_stream.recv_message.return_value = req_msg

        svc.handle_request = AsyncMock()  # type: ignore[method-assign]

        async def _respond():
            await asyncio.sleep(0.01)
            if b"123456789012" in svc.ipc_requests:
                q = svc.ipc_requests[b"123456789012"]
                await q.put(pb.CloudQueuedPublish(topic_name="br/d/13/read/res", payload=b"1"))

        asyncio.create_task(_respond())
        await local_svc.Publish(mock_stream)
        mock_stream.send_message.assert_awaited()
    finally:
        state.cleanup()


@pytest.mark.asyncio
async def test_local_bridge_service_subscribe_console() -> None:
    from mcubridge.state.context import create_runtime_state
    import time
    import os

    fs_root = f".tmp_tests/mcubridge-test-fs-{os.getpid()}-{time.time_ns()}"
    spool_dir = f".tmp_tests/mcubridge-test-spool-{os.getpid()}-{time.time_ns()}"
    config = RuntimeConfig(
        allowed_commands=("echo", "ls"),
        serial_shared_secret=b"testshared",
        file_system_root=fs_root,
        cloud_spool_dir=spool_dir,
        allow_non_tmp_paths=True,
    )
    state = create_runtime_state(config)
    try:
        mock_serial = AsyncMock(spec=SerialTransport)
        svc = BridgeService(config, state, mock_serial)
        local_svc = LocalBridgeService(svc)

        mock_stream = AsyncMock()
        mock_stream.recv_message.return_value = pb.SubscribeRequest()
        mock_stream.send_message.side_effect = OSError("Connection reset")

        q_msg = pb.CloudQueuedPublish(topic_name="br/console/out", payload=b"hello console")

        async def _push():
            await asyncio.sleep(0.01)
            if svc.console_queues:
                await svc.console_queues[0].put(q_msg)

        asyncio.create_task(_push())
        with pytest.raises(OSError):
            await local_svc.SubscribeConsole(mock_stream)
    finally:
        state.cleanup()


@pytest.mark.asyncio
async def test_process_terminate_sigkill_escalation() -> None:
    mock_ctx = MagicMock()
    mock_ctx.handle.returncode = None
    mock_ctx.handle.pid = 999999

    with patch("os.killpg") as mock_kill:
        mock_ctx.handle.wait = AsyncMock(side_effect=TimeoutError("Grace period exceeded"))
        code = await BridgeService._terminate_process(MagicMock(), 999999, mock_ctx, grace_period=0.01)
        assert code == -1
        assert mock_kill.call_count == 2
