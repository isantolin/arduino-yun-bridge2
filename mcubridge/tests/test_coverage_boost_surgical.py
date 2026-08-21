# pyright: reportPrivateUsage=false
"""Surgical unit tests boosting coverage across runtime.py, serial.py, and pin_rest_cgi.py."""

from __future__ import annotations
from mcubridge.transport.serial import SerialTransport
from mcubridge.services.runtime import BridgeService, LocalBridgeService
from mcubridge.protocol import mcubridge_pb2 as pb
from mcubridge.config.settings import RuntimeConfig
from mcubridge.state.context import RuntimeState

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


def test_pin_rest_cgi_set_pin_digital_sync_error() -> None:
    with patch("grpclib.client.Channel", side_effect=OSError("IPC Connection Failed")):
        with pytest.raises(OSError):
            pin_rest_cgi.set_pin_digital_sync(13, 1)


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

    with patch.object(pin_rest_cgi, "set_pin_digital_sync") as mock_set_pin:
        res = pin_rest_cgi.application(env, start_response)
        assert res
        start_response.assert_called_once()
        mock_set_pin.assert_called_once_with(13, 1)

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
async def test_process_poll_stream_timeout(runtime_config: RuntimeConfig, runtime_state: RuntimeState) -> None:
    mock_serial = AsyncMock(spec=SerialTransport)
    svc = BridgeService(runtime_config, runtime_state, mock_serial)

    mock_proc = MagicMock()
    mock_proc.pid = 8888
    mock_proc.returncode = None

    mock_stdout = MagicMock()
    mock_stdout.at_eof.return_value = False
    mock_stdout.read = AsyncMock(return_value=b"partial data")
    mock_proc.stdout = mock_stdout
    mock_proc.stderr = None

    ctx = MagicMock()
    ctx.handle = mock_proc
    ctx.exit_code = 0
    ctx.io_lock = asyncio.Lock()

    runtime_state.running_processes[8888] = ctx

    res = await svc._poll_process(8888)
    assert res.finished is False
    assert res.stdout_truncated is True


@pytest.mark.asyncio
async def test_connect_cloud_session(runtime_config: RuntimeConfig, runtime_state: RuntimeState) -> None:
    mock_serial = AsyncMock(spec=SerialTransport)
    runtime_config.cloud_http3_enabled = True
    svc = BridgeService(runtime_config, runtime_state, mock_serial)

    envelope_pong = MagicMock()
    envelope_pong.WhichOneof.return_value = "pong"

    envelope_cmd = MagicMock()
    envelope_cmd.WhichOneof.return_value = "command_request"
    envelope_cmd.command_request.command_path = "system/version/get"
    envelope_cmd.command_request.payload = b""
    envelope_cmd.sequence_id = 1234

    class AsyncStreamMock:
        def __aiter__(self):
            async def _gen():
                yield envelope_pong
                yield envelope_cmd

            return _gen()

    mock_stream = AsyncStreamMock()

    mock_open_ctx = AsyncMock()
    mock_open_ctx.__aenter__.return_value = mock_stream
    mock_open_ctx.__aexit__.return_value = None

    with (
        patch("mcubridge.services.runtime.Channel"),
        patch("mcubridge.services.runtime.CloudBridgeStub") as mock_stub_cls,
        patch.object(svc, "_send_cloud_event", new_callable=AsyncMock),
        patch.object(svc, "flush_cloud_spool", new_callable=AsyncMock),
    ):
        mock_stub = MagicMock()
        mock_stub.Session.open.return_value = mock_open_ctx
        mock_stub_cls.return_value = mock_stub

        await svc.connect_cloud_session(None)
        assert runtime_state.connected_via_http3 is True


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
