"""Unit tests for the mcubridge-gateway gRPC server and CLI interface."""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any, cast
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from typer.testing import CliRunner

from gateway import (
    CloudBridgeService,
    GatewaySessionMachine,
    GatewaySessionState,
    ProtobufGateway,
    auth_interceptor,
    app,
    extract_peer_identity,
)
from mcubridge.protocol import mcubridge_pb2 as pb


@pytest.fixture
def mock_gateway() -> ProtobufGateway:
    return ProtobufGateway(host="127.0.0.1", port=8443, use_tls=False)


@pytest.fixture
def cloud_service(mock_gateway: ProtobufGateway) -> CloudBridgeService:
    return CloudBridgeService(mock_gateway)


@pytest.mark.asyncio
async def test_session_ping_pong(cloud_service: CloudBridgeService) -> None:
    mock_stream: AsyncMock = AsyncMock()
    mock_stream.peer = MagicMock()
    mock_stream.peer.addr.return_value = ("127.0.0.1", 54321)
    mock_stream.peer.cert.return_value = None

    ping_envelope = pb.CloudEnvelope(
        protocol_version=2,
        device_id="DEV_001",
        sequence_id=10,
        ping=pb.KeepalivePing(interval_ms=1000),
    )

    async def async_iter():
        yield ping_envelope

    def _aiter(self: object):
        return async_iter()

    mock_stream.__aiter__ = _aiter

    await cloud_service.Session(mock_stream)

    assert mock_stream.send_message.called
    sent_pong = mock_stream.send_message.call_args[0][0]
    assert sent_pong.sequence_id == 10
    assert sent_pong.WhichOneof("payload") == "pong"


@pytest.mark.asyncio
async def test_session_client_cert_common_name(cloud_service: CloudBridgeService) -> None:
    mock_stream: AsyncMock = AsyncMock()
    mock_stream.peer = MagicMock()
    mock_stream.peer.addr.return_value = ("10.0.0.1", 12345)
    mock_stream.peer.cert.return_value = {
        "subject": [
            [("countryName", "US")],
            [("commonName", "device-mips-01")],
        ]
    }

    async def async_iter():
        yield pb.CloudEnvelope(protocol_version=2, telemetry=pb.TelemetryReport(daemon_metrics_blob=b"data"))

    def _aiter(self: object):
        return async_iter()

    mock_stream.__aiter__ = _aiter

    await cloud_service.Session(mock_stream)
    assert "device-mips-01" not in cloud_service.gateway.connections


@pytest.mark.asyncio
async def test_session_event_and_command_response(cloud_service: CloudBridgeService) -> None:
    mock_stream: AsyncMock = AsyncMock()
    mock_stream.peer = MagicMock()
    mock_stream.peer.addr.return_value = ("127.0.0.1", 9999)
    mock_stream.peer.cert.return_value = None

    event_envelope = pb.CloudEnvelope(
        protocol_version=2,
        event=pb.EventNotification(event_type="boot", severity="info", description="MCU reset"),
    )
    cmd_resp_envelope = pb.CloudEnvelope(
        protocol_version=2,
        command_response=pb.CommandResponse(status_code=200, error_message="", payload=b""),
    )

    async def async_iter():
        yield event_envelope
        yield cmd_resp_envelope

    def _aiter(self: object):
        return async_iter()

    mock_stream.__aiter__ = _aiter

    await cloud_service.Session(mock_stream)
    assert len(cloud_service.gateway.connections) == 0


def test_protobuf_gateway_ssl_context_disabled() -> None:
    gw = ProtobufGateway(use_tls=False)
    assert gw.get_ssl_context() is None


def test_protobuf_gateway_ssl_context_missing_files() -> None:
    gw = ProtobufGateway(use_tls=True, cert_file=None, key_file=None)
    assert gw.get_ssl_context() is None


def test_protobuf_gateway_ssl_context_valid(tmp_path: Path) -> None:
    cert_file = tmp_path / "server.crt"
    key_file = tmp_path / "server.key"
    cert_file.write_text("dummy cert")
    key_file.write_text("dummy key")

    gw = ProtobufGateway(use_tls=True, cert_file=str(cert_file), key_file=str(key_file))
    with patch("ssl.create_default_context") as mock_ssl_ctx:
        mock_ctx = MagicMock()
        mock_ssl_ctx.return_value = mock_ctx
        ctx = gw.get_ssl_context()
        assert ctx is mock_ctx
        assert mock_ctx.load_cert_chain.called


def test_cli_help() -> None:
    runner = CliRunner()
    result = runner.invoke(cast(Any, app), ["--help"])
    assert result.exit_code == 0
    assert "MCU Bridge Protobuf Gateway" in result.stdout


@pytest.mark.asyncio
async def test_session_unhandled_and_oserror(cloud_service: CloudBridgeService) -> None:
    mock_stream: AsyncMock = AsyncMock()
    mock_stream.peer = MagicMock()
    mock_stream.peer.addr.return_value = ("127.0.0.1", 1234)
    mock_stream.peer.cert.return_value = None

    async def async_iter():
        yield pb.CloudEnvelope(protocol_version=2)
        raise OSError("Connection reset by peer")

    def _aiter(self: object):
        return async_iter()

    mock_stream.__aiter__ = _aiter
    await cloud_service.Session(mock_stream)
    assert len(cloud_service.gateway.connections) == 0


def test_protobuf_gateway_mtls(tmp_path: Path) -> None:
    cert_file = tmp_path / "server.crt"
    key_file = tmp_path / "server.key"
    ca_file = tmp_path / "ca.crt"
    cert_file.write_text("cert")
    key_file.write_text("key")
    ca_file.write_text("ca")

    gw = ProtobufGateway(
        use_tls=True,
        cert_file=str(cert_file),
        key_file=str(key_file),
        ca_file=str(ca_file),
    )
    with patch("ssl.create_default_context") as mock_ssl_ctx:
        mock_ctx = MagicMock()
        mock_ssl_ctx.return_value = mock_ctx
        ctx = gw.get_ssl_context()
        assert ctx is mock_ctx
        assert mock_ctx.load_verify_locations.called


@pytest.mark.asyncio
async def test_protobuf_gateway_run() -> None:
    gw = ProtobufGateway(use_tls=False)
    with patch("gateway.Server") as mock_server_cls:
        mock_server = AsyncMock()
        mock_server.__dispatch__ = MagicMock()
        mock_server_cls.return_value = mock_server
        await gw.run()
        assert mock_server.start.called
        assert mock_server.wait_closed.called


def test_cli_main_invocation() -> None:
    runner = CliRunner()

    def _mock_run(coro: Any) -> None:
        if hasattr(coro, "close"):
            coro.close()

    with patch("asyncio.run", side_effect=_mock_run):
        result = runner.invoke(cast(Any, app), ["--no-tls", "--port", "9090"])
        assert result.exit_code == 0


def test_cli_main_keyboard_interrupt() -> None:
    runner = CliRunner()

    def _mock_run_interrupt(coro: Any) -> None:
        if hasattr(coro, "close"):
            coro.close()
        raise KeyboardInterrupt

    mock_logger_info = MagicMock()
    with (
        patch("asyncio.run", side_effect=_mock_run_interrupt),
        patch("gateway.logger.info", mock_logger_info),
    ):
        result = runner.invoke(cast(Any, app), ["--no-tls", "--http3"])
        assert result.exit_code == 0
        mock_logger_info.assert_called_once_with("Gateway terminated by user.")


@pytest.mark.asyncio
async def test_protobuf_gateway_http3_run() -> None:
    gw = ProtobufGateway(use_tls=False, http3_enabled=True, http3_port=9999)
    with patch("gateway.Server") as mock_server_cls:
        mock_server = AsyncMock()
        mock_server.__dispatch__ = MagicMock()
        mock_server_cls.return_value = mock_server
        await gw.run()
        assert mock_server.start.called


@pytest.mark.asyncio
async def test_cloud_bridge_service_cert_parse_error() -> None:
    gw = ProtobufGateway(use_tls=False)
    service = CloudBridgeService(gw)

    mock_stream = AsyncMock()
    mock_stream.peer = MagicMock()
    mock_stream.peer.addr.return_value = ("127.0.0.1", 12345)
    # Return invalid cert subject structure to trigger parsing error
    mock_stream.peer.cert.return_value = {"subject": [None]}

    await service.Session(mock_stream)
    assert len(gw.connections) == 0


@pytest.mark.asyncio
async def test_cloud_bridge_service_session_cancelled() -> None:
    gw = ProtobufGateway(use_tls=False)
    service = CloudBridgeService(gw)
    mock_stream = AsyncMock()
    mock_stream.peer = MagicMock()
    mock_stream.peer.addr.return_value = ("127.0.0.1", 12345)
    mock_stream.peer.cert.return_value = None
    mock_stream.__aiter__.side_effect = asyncio.CancelledError()
    with pytest.raises(asyncio.CancelledError):
        await service.Session(mock_stream)


@pytest.mark.asyncio
async def test_session_invalid_envelope_validation(cloud_service: CloudBridgeService) -> None:
    mock_stream: AsyncMock = AsyncMock()
    mock_stream.peer = MagicMock()
    mock_stream.peer.addr.return_value = ("127.0.0.1", 54321)
    mock_stream.peer.cert.return_value = None

    # Invalid envelope with wrong protocol_version (fails native check)
    invalid_envelope = pb.CloudEnvelope(protocol_version=99, device_id="DEV_001")

    async def async_iter():
        yield invalid_envelope

    def _aiter(self: object):
        return async_iter()

    mock_stream.__aiter__ = _aiter

    await cloud_service.Session(mock_stream)
    assert not mock_stream.send_message.called


def test_gateway_main_block_simulation() -> None:
    import runpy
    import sys

    with patch.object(sys, "argv", ["gateway.py", "--help"]):
        with pytest.raises(SystemExit):
            runpy.run_path("mcubridge-gateway/gateway.py", run_name="__main__")


def test_gateway_session_machine_lifecycle() -> None:
    # 1. Full authenticated lifecycle: connected -> authenticated -> active -> closed
    fsm = GatewaySessionMachine()
    assert fsm.current_state_value == GatewaySessionState.CONNECTED.value
    assert fsm.connected.is_active

    fsm.authenticate()
    assert fsm.current_state_value == GatewaySessionState.AUTHENTICATED.value
    assert fsm.authenticated.is_active

    fsm.activate()
    assert fsm.current_state_value == GatewaySessionState.ACTIVE.value
    assert fsm.active.is_active

    fsm.close()
    assert fsm.current_state_value == GatewaySessionState.CLOSED.value
    assert fsm.closed.is_active

    # Idempotent close does not raise
    fsm.close()
    assert fsm.current_state_value == GatewaySessionState.CLOSED.value

    # 2. Direct activation lifecycle: connected -> active -> closed
    fsm2 = GatewaySessionMachine()
    fsm2.activate()
    assert fsm2.current_state_value == GatewaySessionState.ACTIVE.value
    fsm2.close()
    assert fsm2.current_state_value == GatewaySessionState.CLOSED.value

    # 3. Direct close: connected -> closed
    fsm3 = GatewaySessionMachine()
    fsm3.close()
    assert fsm3.current_state_value == GatewaySessionState.CLOSED.value


@pytest.mark.asyncio
async def test_gateway_payload_dispatch_empty_or_unhandled(cloud_service: CloudBridgeService) -> None:
    mock_stream: AsyncMock = AsyncMock()
    mock_stream.peer = MagicMock()
    mock_stream.peer.addr.return_value = ("127.0.0.1", 55555)
    mock_stream.peer.cert.return_value = None

    empty_envelope = pb.CloudEnvelope(
        protocol_version=2,
        device_id="DEV_EMPTY",
        sequence_id=99,
    )

    async def async_iter():
        yield empty_envelope

    def _aiter(self: object) -> Any:
        return async_iter()

    mock_stream.__aiter__ = _aiter

    await cloud_service.Session(mock_stream)
    assert not mock_stream.send_message.called


def test_extract_peer_identity() -> None:
    # 1. peer is None
    device_id, auth = extract_peer_identity(None)
    assert device_id == "anonymous-unknown"
    assert auth is False

    # 2. peer with address only
    mock_peer = MagicMock()
    mock_peer.addr.return_value = ("192.168.1.50", 44321)
    mock_peer.cert.return_value = None
    device_id, auth = extract_peer_identity(mock_peer)
    assert device_id == "anonymous-192.168.1.50:44321"
    assert auth is False

    # 3. peer with CN certificate
    mock_peer.cert.return_value = {
        "subject": [
            [("countryName", "US")],
            [("commonName", "test-device-01")],
        ]
    }
    device_id, auth = extract_peer_identity(mock_peer)
    assert device_id == "test-device-01"
    assert auth is True

    # 4. peer with malformed cert subject
    mock_peer.cert.return_value = {"subject": [None]}
    with pytest.raises(ValueError, match="Failed to parse client certificate"):
        extract_peer_identity(mock_peer)

    # 5. peer with valid cert subject but missing commonName
    mock_peer.cert.return_value = {
        "subject": [
            [("countryName", "US")],
            [("organizationName", "Acme Inc")],
        ]
    }
    device_id_no_cn, auth_no_cn = extract_peer_identity(mock_peer)
    assert device_id_no_cn == "anonymous-192.168.1.50:44321"
    assert auth_no_cn is False


@pytest.mark.asyncio
async def test_auth_interceptor_flow() -> None:
    # Valid call flow through interceptor
    called_with_stream = False

    async def dummy_handler(stream: Any) -> None:
        nonlocal called_with_stream
        called_with_stream = True

    mock_event = MagicMock()
    mock_event.method_func = dummy_handler
    mock_event.method_name = "Session"
    mock_event.peer = MagicMock()
    mock_event.peer.addr.return_value = ("10.0.0.5", 8080)
    mock_event.peer.cert.return_value = {"subject": [[("commonName", "auth-device-99")]]}

    await auth_interceptor(mock_event)
    assert mock_event.method_func != dummy_handler

    # Invoke the wrapped handler
    mock_stream = AsyncMock()
    await mock_event.method_func(mock_stream)
    assert called_with_stream is True

    # Error handling when cert is invalid
    mock_event_bad = MagicMock()
    mock_event_bad.method_func = dummy_handler
    mock_event_bad.peer = MagicMock()
    mock_event_bad.peer.addr.return_value = ("10.0.0.5", 8080)
    mock_event_bad.peer.cert.return_value = {"subject": [None]}

    await auth_interceptor(mock_event_bad)
    # When cert is invalid, interceptor does not wrap and returns early
    assert mock_event_bad.method_func == dummy_handler
