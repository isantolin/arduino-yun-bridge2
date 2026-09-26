"""Surgical unit tests boosting coverage across runtime.py, serial.py, and pin_rest_cgi.py."""

from __future__ import annotations

import asyncio
import importlib.util
import sys
import types
from io import BytesIO
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest
from mcubridge.protocol import mcubridge_pb2 as pb
from mcubridge.services.runtime import BridgeService, LocalBridgeService
from pytest_mock import MockerFixture

# Ensure "uci" mock exists before importing pin_rest_cgi
if "uci" not in sys.modules:
    sys.modules["uci"] = types.ModuleType("uci")

if "pin_rest_cgi" in sys.modules:
    pin_rest_cgi = sys.modules["pin_rest_cgi"]
else:
    script_path = Path(__file__).parent.parent / "scripts" / "pin_rest_cgi.py"
    spec = importlib.util.spec_from_file_location("pin_rest_cgi", str(script_path))
    assert spec is not None and spec.loader is not None
    pin_rest_cgi = importlib.util.module_from_spec(spec)
    sys.modules["pin_rest_cgi"] = pin_rest_cgi
    spec.loader.exec_module(pin_rest_cgi)


def test_pin_rest_cgi_set_pin_digital_sync_error(mocker: MockerFixture) -> None:
    mocker.patch.object(pin_rest_cgi, "ubus", None)
    with pytest.raises(RuntimeError, match="Native OpenWrt UBUS module unavailable"):
        pin_rest_cgi.set_pin_digital_sync(13, 1)

    mock_ubus = MagicMock()
    mock_ubus.call.side_effect = OSError("UBUS failure")
    mocker.patch.object(pin_rest_cgi, "ubus", mock_ubus)
    with pytest.raises(OSError, match="UBUS failure"):
        pin_rest_cgi.set_pin_digital_sync(13, 1)
    assert mock_ubus.connect.called
    mock_ubus.call.assert_called_once_with("mcubridge", "digital_write", {"pin": 13, "value": 1})

    mock_ubus_ok = MagicMock()
    mocker.patch.object(pin_rest_cgi, "ubus", mock_ubus_ok)
    pin_rest_cgi.set_pin_digital_sync(13, 1)
    assert mock_ubus_ok.connect.called
    mock_ubus_ok.call.assert_called_once_with("mcubridge", "digital_write", {"pin": 13, "value": 1})


def test_pin_rest_cgi_application(mocker: MockerFixture) -> None:
    start_response = MagicMock()
    body = b'{"state": "ON"}'
    env = {
        "PATH_INFO": "/pin/13",
        "REQUEST_METHOD": "POST",
        "CONTENT_LENGTH": str(len(body)),
        "wsgi.input": BytesIO(body),
    }

    mock_set_pin = mocker.patch.object(pin_rest_cgi, "set_pin_digital_sync")
    res = pin_rest_cgi.application(env, start_response)
    assert res
    start_response.assert_called_once()
    mock_set_pin.assert_called_once_with(13, 1)

    start_response_err = MagicMock()
    env_invalid = {"PATH_INFO": "/invalid", "REQUEST_METHOD": "GET"}
    res_err = pin_rest_cgi.application(env_invalid, start_response_err)
    assert res_err
    start_response_err.assert_called_with(
        "400 Bad Request",
        [
            ("Content-Type", "application/json"),
            ("Content-Length", "52"),
            ("Access-Control-Allow-Origin", "*"),
            ("Access-Control-Allow-Methods", "GET, POST, OPTIONS"),
            ("Access-Control-Allow-Headers", "Content-Type"),
        ],
    )


@pytest.mark.asyncio
async def test_local_bridge_service_ipc(mock_bridge_service: BridgeService) -> None:
    svc = mock_bridge_service
    local_svc = LocalBridgeService(svc)

    # Test Publish with no stream message
    mock_stream = AsyncMock()
    mock_stream.recv_message.return_value = None
    await local_svc.Publish(mock_stream)
    mock_stream.send_message.assert_not_called()

    # Test Publish with message & correlation
    req_msg = pb.CloudQueuedPublish(topic_name="br/d/13/read", correlation_data=b"123456789012")
    mock_stream.recv_message.return_value = req_msg
    setattr(svc, "handle_request", AsyncMock())

    async def _respond() -> None:
        await asyncio.sleep(0.01)
        if b"123456789012" in svc.ipc_requests:
            q = svc.ipc_requests[b"123456789012"]
            await q.put(pb.CloudQueuedPublish(topic_name="br/d/13/read/res", payload=b"1"))

    asyncio.create_task(_respond())
    await local_svc.Publish(mock_stream)
    mock_stream.send_message.assert_awaited()


@pytest.mark.asyncio
async def test_local_bridge_service_subscribe_console(mock_bridge_service: BridgeService) -> None:
    svc = mock_bridge_service
    local_svc = LocalBridgeService(svc)

    mock_stream = AsyncMock()
    mock_stream.recv_message.return_value = pb.SubscribeRequest()
    mock_stream.send_message.side_effect = OSError("Connection reset")

    q_msg = pb.CloudQueuedPublish(topic_name="br/console/out", payload=b"hello console")

    async def _push() -> None:
        await asyncio.sleep(0.01)
        if svc.console_queues:
            await svc.console_queues[0].put(q_msg)

    asyncio.create_task(_push())
    with pytest.raises(OSError):
        await local_svc.SubscribeConsole(mock_stream)


@pytest.mark.asyncio
async def test_process_poll_stream_timeout(mock_bridge_service: BridgeService) -> None:
    svc = mock_bridge_service
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

    svc.state.running_processes[8888] = ctx

    res = await svc.poll_process(8888)
    assert res.finished is False
    assert res.stdout_truncated is True


@pytest.mark.asyncio
async def test_connect_cloud_session(mock_bridge_service: BridgeService, mocker: MockerFixture) -> None:
    svc = mock_bridge_service
    svc.config.cloud_http3_enabled = True

    envelope_pong = MagicMock()
    envelope_pong.WhichOneof.return_value = "pong"

    envelope_cmd = MagicMock()
    envelope_cmd.WhichOneof.return_value = "command_request"
    envelope_cmd.command_request.command_path = "rpc/GetVersion"
    envelope_cmd.command_request.payload = b""
    envelope_cmd.sequence_id = 1234

    class AsyncStreamMock:
        def __init__(self) -> None:
            self.send_message = AsyncMock()

        def __aiter__(self):
            async def _gen():
                yield envelope_pong
                yield envelope_cmd

            return _gen()

    mock_stream = AsyncStreamMock()

    mock_open_ctx = AsyncMock()
    mock_open_ctx.__aenter__.return_value = mock_stream
    mock_open_ctx.__aexit__.return_value = None

    mocker.patch("mcubridge.services.runtime.Channel")
    mock_stub_cls = mocker.patch("mcubridge.services.runtime.CloudBridgeStub")
    mocker.patch.object(svc, "_send_cloud_event", new_callable=AsyncMock)
    mocker.patch.object(svc, "flush_cloud_spool", new_callable=AsyncMock)

    mock_stub = MagicMock()
    mock_stub.Session.open.return_value = mock_open_ctx
    mock_stub_cls.return_value = mock_stub

    await svc.connect_cloud_session(None)
    assert svc.state.connected_via_http3 is True
    mock_stream.send_message.assert_awaited_once()
    resp = mock_stream.send_message.call_args[0][0]
    assert resp.sequence_id == 1234
    assert resp.command_response.status_code == 200


@pytest.mark.asyncio
async def test_process_terminate_sigkill_escalation(mocker: MockerFixture) -> None:
    mock_ctx = MagicMock()
    mock_ctx.handle.returncode = None
    mock_ctx.handle.pid = 999999

    mock_term = mocker.patch("mcubridge.services.runtime.terminate_pid_tree")
    from collections.abc import Awaitable, Callable

    terminate_proc: Callable[..., Awaitable[int]] = getattr(BridgeService, "_terminate_process")
    code = await terminate_proc(MagicMock(), 999999, mock_ctx, grace_period=0.5)
    mock_term.assert_called_once_with(999999, timeout=0.5)
    assert code == -1
