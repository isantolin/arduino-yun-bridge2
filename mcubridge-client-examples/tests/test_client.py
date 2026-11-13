import asyncio
import socket
import ssl
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any, cast
from unittest.mock import AsyncMock, MagicMock

import pw_hdlc.encode
import pytest
from mcubridge_client import GatewayClient, GatewayConnection, cloud_gateway_pb2, config_schema_pb2, mcu_wire_pb2
from mcubridge_client.gateway import gateway_control_pb2
from pw_status import Status as PwStatus
from mcubridge_client.definitions import (
    build_bridge_args,
)
from mcubridge_client.env import dump_client_env, is_openwrt, read_uci_general
from mcubridge_client.protocol import CLOUD_DEFAULT_TOPIC_PREFIX, DEFAULT_CLOUD_HOST, DEFAULT_CLOUD_PORT, Topic
from mcubridge_client.spi import SpiDevice


@pytest.fixture
def mock_grpc() -> tuple[MagicMock, MagicMock]:
    mock_channel = MagicMock(spec=GatewayConnection)

    mock_stub = MagicMock(spec=GatewayClient)
    mock_stub.DigitalWrite = AsyncMock()
    mock_stub.AnalogWrite = AsyncMock()
    mock_stub.DatastorePut = AsyncMock()
    mock_stub.FileWrite = AsyncMock()
    mock_stub.AnalogRead = AsyncMock()
    mock_stub.PinSubscribe = AsyncMock()
    return mock_channel, mock_stub


async def test_client_connect_disconnect(mock_grpc: tuple[MagicMock, MagicMock]) -> None:
    mock_channel, mock_stub = mock_grpc
    assert mock_channel is not None
    assert mock_stub is not None


async def test_client_digital_write(mock_grpc: tuple[MagicMock, MagicMock]) -> None:
    _, mock_stub = mock_grpc
    msg = mcu_wire_pb2.DigitalWrite(pin=13, value=1)
    await mock_stub.DigitalWrite(msg)
    mock_fn = cast(AsyncMock, mock_stub.DigitalWrite)
    mock_fn.assert_awaited_once_with(msg)
    call_args = mock_fn.call_args
    assert call_args is not None
    sent = cast(mcu_wire_pb2.DigitalWrite, call_args[0][0])
    assert sent.pin == 13
    assert sent.value == 1


async def test_client_analog_write(mock_grpc: tuple[MagicMock, MagicMock]) -> None:
    _, mock_stub = mock_grpc
    msg = mcu_wire_pb2.AnalogWrite(pin=3, value=128)
    await mock_stub.AnalogWrite(msg)
    mock_fn = cast(AsyncMock, mock_stub.AnalogWrite)
    mock_fn.assert_awaited_once_with(msg)
    call_args = mock_fn.call_args
    assert call_args is not None
    sent = cast(mcu_wire_pb2.AnalogWrite, call_args[0][0])
    assert sent.pin == 3
    assert sent.value == 128


async def test_client_datastore_put(mock_grpc: tuple[MagicMock, MagicMock]) -> None:
    _, mock_stub = mock_grpc
    mock_fn = cast(AsyncMock, mock_stub.DatastorePut)
    mock_fn.return_value = cloud_gateway_pb2.GenericResponse(status="ok")
    msg = mcu_wire_pb2.DatastorePut(key="test_key", value=b"test_value")
    res = cast(cloud_gateway_pb2.GenericResponse, await mock_stub.DatastorePut(msg))
    mock_fn.assert_awaited_once_with(msg)
    assert res.status == "ok"
    call_args = mock_fn.call_args
    assert call_args is not None
    sent = cast(mcu_wire_pb2.DatastorePut, call_args[0][0])
    assert sent.key == "test_key"
    assert sent.value == b"test_value"


async def test_client_file_write(mock_grpc: tuple[MagicMock, MagicMock]) -> None:
    _, mock_stub = mock_grpc
    msg = mcu_wire_pb2.FileWrite(path="test.txt", data=b"content")
    await mock_stub.FileWrite(msg)
    mock_fn = cast(AsyncMock, mock_stub.FileWrite)
    mock_fn.assert_awaited_once_with(msg)
    call_args = mock_fn.call_args
    assert call_args is not None
    sent = cast(mcu_wire_pb2.FileWrite, call_args[0][0])
    assert sent.path == "test.txt"
    assert sent.data == b"content"


async def test_client_analog_read_timeout(mock_grpc: tuple[MagicMock, MagicMock]) -> None:
    _, mock_stub = mock_grpc
    mock_fn = cast(AsyncMock, mock_stub.AnalogRead)
    mock_fn.side_effect = TimeoutError("RPC timeout")
    msg = mcu_wire_pb2.PinRead(pin=0)
    with pytest.raises(TimeoutError):
        await mock_stub.AnalogRead(msg)


async def test_client_pin_subscribe(mock_grpc: tuple[MagicMock, MagicMock]) -> None:
    _, mock_stub = mock_grpc
    mock_fn = cast(AsyncMock, mock_stub.PinSubscribe)
    mock_fn.return_value = mcu_wire_pb2.PinSubscribeResponse(pin=13, success=True)
    msg = mcu_wire_pb2.PinSubscribeRequest(
        pin=13,
        mode=config_schema_pb2.PinModeType.PIN_INPUT,
        interval_ms=100,
        hysteresis=1,
        enabled=True,
    )
    res = cast(mcu_wire_pb2.PinSubscribeResponse, await mock_stub.PinSubscribe(msg))
    mock_fn.assert_awaited_once_with(msg)
    assert res.pin == 13
    assert res.success is True
    call_args = mock_fn.call_args
    assert call_args is not None
    sent = cast(mcu_wire_pb2.PinSubscribeRequest, call_args[0][0])
    assert sent.pin == 13
    assert sent.interval_ms == 100
    assert sent.enabled is True


def test_definitions_build_bridge_args() -> None:
    # With topic_prefix
    args1 = build_bridge_args(host="10.0.0.1", port=9000, device_id="dev1", topic_prefix="custom")
    assert args1["topic_prefix"] == "custom"
    assert args1["device_id"] == "dev1"

    # Without topic_prefix (empty string triggers False branch)
    args2 = build_bridge_args(host="10.0.0.1", port=9000, device_id="dev1", topic_prefix="")
    assert "topic_prefix" not in args2
    assert args2["device_id"] == "dev1"


def test_client_defaults_match_generated_protocol(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("MCUBRIDGE_GATEWAY_HOST", raising=False)
    monkeypatch.delenv("MCUBRIDGE_CLOUD_HOST", raising=False)
    monkeypatch.delenv("MCUBRIDGE_GATEWAY_PORT", raising=False)
    monkeypatch.delenv("MCUBRIDGE_CLOUD_PORT", raising=False)

    assert Topic.PREFIX == CLOUD_DEFAULT_TOPIC_PREFIX
    args = build_bridge_args(device_id="dev1")
    assert args["host"] == DEFAULT_CLOUD_HOST
    assert args["port"] == DEFAULT_CLOUD_PORT
    assert args["topic_prefix"] == CLOUD_DEFAULT_TOPIC_PREFIX


def test_env_functions(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    # 1. is_openwrt with forced env and real file
    monkeypatch.setenv("MCUBRIDGE_FORCE_UCI", "1")
    assert is_openwrt() is True

    monkeypatch.delenv("MCUBRIDGE_FORCE_UCI", raising=False)
    fake_release = tmp_path / "openwrt_release"
    fake_release.touch()
    assert is_openwrt(release_file=fake_release) is True
    assert is_openwrt(release_file=tmp_path / "nonexistent", version_file=tmp_path / "nonexistent2") is False

    # 2. read_uci_general when not openwrt
    assert read_uci_general() == {}

    # 2b. read_uci_general when openwrt but find_spec is None
    def _mock_find_spec(_name: str) -> None:
        return None

    monkeypatch.setenv("MCUBRIDGE_FORCE_UCI", "1")
    monkeypatch.setattr("importlib.util.find_spec", _mock_find_spec)
    assert read_uci_general() == {}

    # 2c. read_uci_general when openwrt and mcubridge.config.common is loaded
    monkeypatch.undo()
    monkeypatch.setenv("MCUBRIDGE_FORCE_UCI", "1")
    uci_res = read_uci_general()
    assert isinstance(uci_res, dict)
    assert "serial_baud" in uci_res
    monkeypatch.delenv("MCUBRIDGE_FORCE_UCI", raising=False)

    # 3. read_uci_general when get_uci_config is not callable
    assert read_uci_general(config_getter=cast(Any, "not_callable")) == {}

    # 4. read_uci_general when get_uci_config raises OSError
    def _raise_oserror() -> dict[str, Any]:
        raise OSError("disk error")

    assert read_uci_general(config_getter=_raise_oserror) == {}

    # 5. read_uci_general with valid config filtering internal keys
    res = read_uci_general(config_getter=lambda: {"port": "50051", ".hidden": "x", "_priv": "y"})
    assert res == {"port": "50051"}

    # 6. dump_client_env
    logs: list[str] = []
    fake_logger = MagicMock()

    def _capture_log(msg: str) -> None:
        logs.append(msg)

    fake_logger.info = _capture_log
    dump_client_env(fake_logger)
    assert len(logs) == 4
    assert "MCU Bridge client configuration" in logs[0]


async def test_spi_device_transfer() -> None:
    MagicMock(spec=GatewayConnection)
    mock_stub = MagicMock(spec=GatewayClient)
    mock_spi_config = AsyncMock()
    mock_stub.SpiConfigure = mock_spi_config
    mock_stub.SpiBegin = AsyncMock()
    mock_stub.SpiEnd = AsyncMock()
    mock_stub.SpiTransfer = AsyncMock(return_value=mcu_wire_pb2.SpiTransfer(data=b"\xaa\xbb"))

    dev = SpiDevice(mock_stub)

    # Transfer with bytes while inactive (should call begin() automatically)
    assert not dev.is_active
    res1 = await dev.transfer(b"\x01\x02")
    assert dev.is_active
    assert res1 == b"\xaa\xbb"
    # The bus must be started on the MCU before it is configured and used.
    mock_stub.SpiBegin.assert_awaited_once()
    mock_spi_config.assert_awaited_once()

    # Transfer with sequence/list of ints while active
    res2 = await dev.transfer([0x03, 0x04])
    assert res2 == b"\xaa\xbb"

    # End releases the bus on the MCU exactly once.
    await dev.end()
    assert not dev.is_active
    mock_stub.SpiEnd.assert_awaited_once()
    await dev.end()
    mock_stub.SpiEnd.assert_awaited_once()


class FakeCall:
    """Stand-in for pw_rpc's callback_client Call: the unary response lives on the call."""

    def __init__(self, response: Any = None) -> None:
        self.response = response


def _fake_create_connection(sock: Any) -> Callable[..., Any]:
    def _connect(_address: Any, timeout: float = 10.0) -> Any:
        return sock

    return _connect


def test_gateway_connection_raw_and_ssl(monkeypatch: pytest.MonkeyPatch) -> None:
    sock_srv, sock_cli = socket.socketpair()
    monkeypatch.setattr(socket, "create_connection", _fake_create_connection(sock_cli))

    # 1. Plain socket connection
    conn = GatewayConnection("127.0.0.1", 8443)
    assert conn.running is True
    conn.send_frame(b"ping")
    raw = sock_srv.recv(1024)
    assert len(raw) > 0

    # Test send_frame when sock is None
    monkeypatch.setattr(conn, "sock", None)
    conn.send_frame(b"ping")
    monkeypatch.setattr(conn, "sock", sock_cli)

    # Test reading valid HDLC frame with address 1
    mock_process = MagicMock()
    conn.rpc_client.process_packet = mock_process
    sock_srv.sendall(pw_hdlc.encode.ui_frame(1, b"\x08\x01"))

    # Give reader thread time to consume
    for _ in range(50):
        if mock_process.called:
            break
        time.sleep(0.01)
    assert mock_process.called

    # Test reading frame with non-rpc address (should be ignored)
    sock_srv.sendall(pw_hdlc.encode.ui_frame(2, b"log_data"))

    # Close connection: the peer (the Gateway) must observe EOF immediately so it
    # releases the client principal before the next session with the same identity.
    conn.close()
    sock_srv.settimeout(1.0)
    assert sock_srv.recv(16) == b""
    sock_srv.close()
    sock_cli.close()

    # 2. Test close error handling
    mock_sock = MagicMock()
    mock_sock.shutdown.side_effect = OSError("not connected")
    mock_sock.close.side_effect = OSError("close error")
    mock_sock.recv.side_effect = OSError("closed")
    monkeypatch.setattr(socket, "create_connection", _fake_create_connection(mock_sock))
    conn_err = GatewayConnection("127.0.0.1", 8443)
    conn_err.close()
    mock_sock.shutdown.assert_called_once_with(socket.SHUT_RDWR)
    mock_sock.close.assert_called_once_with()

    # 3. SSL wrapped connection
    mock_ssl_ctx = MagicMock(spec=ssl.SSLContext)
    mock_wrapped_sock = MagicMock()
    mock_ssl_ctx.wrap_socket.return_value = mock_wrapped_sock
    mock_wrapped_sock.recv.side_effect = OSError("closed")

    monkeypatch.setattr(socket, "create_connection", _fake_create_connection(MagicMock()))
    conn_ssl = GatewayConnection("127.0.0.1", 8443, ssl_context=mock_ssl_ctx)
    mock_ssl_ctx.wrap_socket.assert_called_once()
    conn_ssl.close()


async def test_gateway_client_dispatch_lifecycle() -> None:
    mock_conn = MagicMock()
    client = GatewayClient(mock_conn, target_device_id="yun-01")

    # 1. Success dispatch
    def _invoke_success(req: Any, on_completed: Any = None, on_error: Any = None) -> None:
        assert req.target_device_id == "yun-01"
        assert req.service_name == "GpioService"
        assert req.method_name == "DigitalWrite"
        resp = gateway_control_pb2.DispatchResponse(
            status_code=0,
            response_payload=mcu_wire_pb2.Empty().SerializeToString(),
        )
        if on_completed:
            on_completed(FakeCall(resp), PwStatus.OK)

    mock_dispatch = MagicMock()
    mock_dispatch.invoke = _invoke_success
    mock_conn.rpc_client.channel.return_value.rpcs.mcubridge.gateway.GatewayControl.Dispatch = mock_dispatch

    res_bytes = await client.dispatch("GpioService", "DigitalWrite", mcu_wire_pb2.DigitalWrite(pin=13, value=1))
    assert res_bytes == mcu_wire_pb2.Empty().SerializeToString()

    # 2. Error dispatch (status not ok)
    def _invoke_error(req: Any, on_completed: Any = None, on_error: Any = None) -> None:
        if on_error:
            on_error(FakeCall(), PwStatus.INTERNAL)

    mock_dispatch.invoke = _invoke_error
    with pytest.raises(RuntimeError, match="Pigweed RPC dispatch failed"):
        await client.dispatch("GpioService", "DigitalWrite")


async def test_gateway_client_health() -> None:
    mock_conn = MagicMock()
    client = GatewayClient(mock_conn, target_device_id="yun-01")

    # 1. Health success
    def _invoke_health_ok(on_completed: Any = None, on_error: Any = None) -> None:
        resp = gateway_control_pb2.HealthResponse(ready=True, active_device_sessions=1, active_client_sessions=1)
        if on_completed:
            on_completed(FakeCall(resp), PwStatus.OK)

    mock_health = MagicMock()
    mock_health.invoke = _invoke_health_ok
    mock_conn.rpc_client.channel.return_value.rpcs.mcubridge.gateway.GatewayControl.Health = mock_health

    health = await client.Health()
    assert health.ready is True
    assert health.active_device_sessions == 1
    assert health.active_client_sessions == 1

    # 2. Health error
    def _invoke_health_err(on_completed: Any = None, on_error: Any = None) -> None:
        if on_error:
            on_error(FakeCall(), PwStatus.UNAVAILABLE)

    mock_health.invoke = _invoke_health_err
    with pytest.raises(RuntimeError, match="Health check failed"):
        await client.Health()


async def test_gateway_client_all_convenience_methods(monkeypatch: pytest.MonkeyPatch) -> None:
    mock_conn = MagicMock()
    client = GatewayClient(mock_conn, target_device_id="yun-01")

    responses: dict[tuple[str, str], Callable[[Any], bytes]] = {
        ("rpc.pb.GpioService", "DigitalWrite"): lambda _req: mcu_wire_pb2.Empty().SerializeToString(),
        ("rpc.pb.GpioService", "DigitalRead"): lambda _req: mcu_wire_pb2.DigitalReadResponse(
            value=1
        ).SerializeToString(),
        ("rpc.pb.GpioService", "AnalogWrite"): lambda _req: mcu_wire_pb2.Empty().SerializeToString(),
        ("rpc.pb.GpioService", "AnalogRead"): lambda _req: mcu_wire_pb2.AnalogReadResponse(
            value=512
        ).SerializeToString(),
        ("rpc.pb.GpioService", "SetPinMode"): lambda _req: mcu_wire_pb2.Empty().SerializeToString(),
        ("rpc.pb.GpioService", "SubscribePins"): lambda req: mcu_wire_pb2.PinSubscribeResponse(
            pin=req.pin, success=True
        ).SerializeToString(),
        ("rpc.pb.ConsoleService", "Write"): lambda _req: mcu_wire_pb2.Empty().SerializeToString(),
        ("rpc.pb.DataStoreService", "Put"): lambda _req: mcu_wire_pb2.Empty().SerializeToString(),
        ("rpc.pb.DataStoreService", "Get"): lambda _req: (
            mcu_wire_pb2.DatastoreGetResponse(value=b"stored").SerializeToString()
        ),
        ("rpc.pb.MailboxService", "Push"): lambda _req: mcu_wire_pb2.Empty().SerializeToString(),
        ("rpc.pb.MailboxService", "Read"): lambda _req: mcu_wire_pb2.MailboxReadResponse(
            content=b"msg"
        ).SerializeToString(),
        ("rpc.pb.ProcessService", "RunAsync"): lambda _req: mcu_wire_pb2.ProcessRunAsyncResponse(
            pid=42
        ).SerializeToString(),
        ("rpc.pb.ProcessService", "Poll"): lambda _req: mcu_wire_pb2.ProcessPollResponse(
            finished=True, exit_code=0
        ).SerializeToString(),
        ("rpc.pb.ProcessService", "Kill"): lambda _req: mcu_wire_pb2.Empty().SerializeToString(),
        ("rpc.pb.FileSystemService", "Write"): lambda _req: mcu_wire_pb2.Empty().SerializeToString(),
        ("rpc.pb.FileSystemService", "Remove"): lambda _req: mcu_wire_pb2.Empty().SerializeToString(),
        ("rpc.pb.SpiService", "Begin"): lambda _req: mcu_wire_pb2.Empty().SerializeToString(),
        ("rpc.pb.SpiService", "End"): lambda _req: mcu_wire_pb2.Empty().SerializeToString(),
        ("rpc.pb.SpiService", "SetConfig"): lambda _req: mcu_wire_pb2.Empty().SerializeToString(),
        ("rpc.pb.SpiService", "Transfer"): lambda req: mcu_wire_pb2.SpiTransferResponse(
            data=req.data
        ).SerializeToString(),
        ("rpc.pb.SystemService", "GetVersion"): lambda _req: mcu_wire_pb2.VersionResponse(
            major=2, minor=8, patch=9
        ).SerializeToString(),
        ("rpc.pb.SystemService", "GetFreeMemory"): lambda _req: (
            mcu_wire_pb2.FreeMemoryResponse(value=1155).SerializeToString()
        ),
        ("rpc.pb.SystemService", "EnterBootloader"): lambda _req: mcu_wire_pb2.Empty().SerializeToString(),
    }

    async def _mock_dispatch(service: str, method: str, req: Any = None, timeout_ms: int = 5000) -> bytes:
        factory = responses.get((service, method))
        if factory is not None:
            return factory(req)
        raise NotImplementedError(f"Unhandled {service}/{method}")

    monkeypatch.setattr(client, "dispatch", _mock_dispatch)

    res_dw = await client.DigitalWrite(mcu_wire_pb2.DigitalWrite(pin=13, value=1))
    assert isinstance(res_dw, mcu_wire_pb2.Empty)

    res_dr = await client.DigitalRead(mcu_wire_pb2.PinRead(pin=13))
    assert res_dr.value == 1

    res_aw = await client.AnalogWrite(mcu_wire_pb2.AnalogWrite(pin=3, value=128))
    assert isinstance(res_aw, mcu_wire_pb2.Empty)

    res_ar = await client.AnalogRead(mcu_wire_pb2.PinRead(pin=0))
    assert res_ar.value == 512

    res_pm = await client.SetPinMode(mcu_wire_pb2.PinMode(pin=13, mode=config_schema_pb2.PinModeType.PIN_OUTPUT))
    assert isinstance(res_pm, mcu_wire_pb2.Empty)

    res_ps = await client.PinSubscribe(mcu_wire_pb2.PinSubscribeRequest(pin=7, enabled=True))
    assert res_ps.pin == 7
    assert res_ps.success is True

    res_cw = await client.ConsoleWrite(mcu_wire_pb2.ConsoleWrite(data=b"ping"))
    assert isinstance(res_cw, mcu_wire_pb2.Empty)

    res_dp = await client.DatastorePut(mcu_wire_pb2.DatastorePut(key="k", value=b"v"))
    assert isinstance(res_dp, mcu_wire_pb2.Empty)

    res_dg = await client.DatastoreGet(mcu_wire_pb2.DatastoreGet(key="k"))
    assert res_dg.value == b"stored"

    res_mp = await client.MailboxPush(mcu_wire_pb2.MailboxPush(data=b"hello"))
    assert isinstance(res_mp, mcu_wire_pb2.Empty)

    res_mr = await client.MailboxRead(mcu_wire_pb2.SubscribeRequest())
    assert res_mr.content == b"msg"

    res_pra = await client.ProcessRunAsync(mcu_wire_pb2.ProcessRunAsync(command="echo hi"))
    assert res_pra.pid == 42

    res_pp = await client.ProcessPoll(mcu_wire_pb2.ProcessPoll(pid=42))
    assert res_pp.finished is True
    assert res_pp.exit_code == 0

    res_pk = await client.ProcessKill(mcu_wire_pb2.ProcessKill(pid=42))
    assert isinstance(res_pk, mcu_wire_pb2.Empty)

    res_fw = await client.FileWrite(mcu_wire_pb2.FileWrite(path="a.txt", data=b"data"))
    assert isinstance(res_fw, mcu_wire_pb2.Empty)

    res_fr = await client.FileRemove(mcu_wire_pb2.FileRemove(path="a.txt"))
    assert isinstance(res_fr, mcu_wire_pb2.Empty)

    assert isinstance(await client.SpiBegin(mcu_wire_pb2.SubscribeRequest()), mcu_wire_pb2.Empty)
    assert isinstance(await client.SpiEnd(mcu_wire_pb2.SubscribeRequest()), mcu_wire_pb2.Empty)

    res_sc = await client.SpiConfigure(mcu_wire_pb2.SpiConfig(frequency=1000000))
    assert isinstance(res_sc, mcu_wire_pb2.Empty)

    res_st = await client.SpiTransfer(mcu_wire_pb2.SpiTransfer(data=b"\xde\xad"))
    assert res_st.data == b"\xde\xad"

    res_ver = await client.GetVersion(mcu_wire_pb2.SubscribeRequest())
    assert res_ver.major == 2
    assert res_ver.minor == 8
    assert res_ver.patch == 9

    res_mem = await client.GetFreeMemory(mcu_wire_pb2.SubscribeRequest())
    assert res_mem.value == 1155

    res_boot = await client.EnterBootloader(mcu_wire_pb2.EnterBootloader(magic=0xDEADBEEF))
    assert isinstance(res_boot, mcu_wire_pb2.Empty)


def test_gateway_connection_reader_eof(monkeypatch: pytest.MonkeyPatch) -> None:
    mock_sock = MagicMock()
    mock_sock.recv.return_value = b""
    monkeypatch.setattr(socket, "create_connection", _fake_create_connection(mock_sock))
    conn = GatewayConnection("127.0.0.1", 8443)
    conn.reader_thread.join(timeout=2)
    assert not conn.reader_thread.is_alive()
    conn.close()


async def test_gateway_client_duplicate_callbacks() -> None:
    mock_conn = MagicMock()
    mock_rpcs = MagicMock()
    mock_conn.rpc_client.channel.return_value.rpcs = mock_rpcs

    captured_completed: Any = None
    captured_error: Any = None

    def mock_invoke(req: Any, on_completed: Any = None, on_error: Any = None) -> None:
        nonlocal captured_completed, captured_error
        captured_completed = on_completed
        captured_error = on_error

    mock_rpcs.mcubridge.gateway.GatewayControl.Dispatch.invoke.side_effect = mock_invoke
    client = GatewayClient(mock_conn, target_device_id="dev-test")

    async def call_dispatch() -> bytes:
        return await client.dispatch("SystemService", "Ping", request=None)

    task = asyncio.create_task(call_dispatch())
    await asyncio.sleep(0.01)
    assert captured_completed is not None
    assert captured_error is not None

    mock_resp = MagicMock()
    mock_resp.response_payload = b"ok"
    call = FakeCall(mock_resp)

    captured_completed(call, PwStatus.OK)
    captured_completed(call, PwStatus.OK)
    captured_error(call, PwStatus.INTERNAL)

    res = await task
    assert res == b"ok"


async def test_gateway_client_health_duplicate_callbacks() -> None:
    mock_conn = MagicMock()
    mock_rpcs = MagicMock()
    mock_conn.rpc_client.channel.return_value.rpcs = mock_rpcs

    captured_completed: Any = None
    captured_error: Any = None

    def mock_invoke(on_completed: Any = None, on_error: Any = None) -> None:
        nonlocal captured_completed, captured_error
        captured_completed = on_completed
        captured_error = on_error

    mock_rpcs.mcubridge.gateway.GatewayControl.Health.invoke.side_effect = mock_invoke
    client = GatewayClient(mock_conn, target_device_id="dev-test")

    task = asyncio.create_task(client.Health())
    await asyncio.sleep(0.01)

    mock_resp = MagicMock()
    call = FakeCall(mock_resp)

    captured_completed(call, PwStatus.OK)
    captured_completed(call, PwStatus.OK)
    captured_error(call, PwStatus.INTERNAL)

    res = await task
    assert res == mock_resp
