"""Unit tests for the mcubridge-gateway gRPC server and CLI interface."""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any, cast
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from typer.testing import CliRunner

from gateway import CloudBridgeService, ProtobufGateway, app
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

    with patch("asyncio.run", side_effect=_mock_run_interrupt):
        result = runner.invoke(cast(Any, app), ["--no-tls", "--http3"])
        assert result.exit_code == 0


@pytest.mark.asyncio
async def test_protobuf_gateway_http3_run() -> None:
    gw = ProtobufGateway(use_tls=False, http3_enabled=True, http3_port=9999)
    with patch("gateway.Server") as mock_server_cls:
        mock_server = AsyncMock()
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
async def test_session_protovalidate_validation_error(cloud_service: CloudBridgeService) -> None:
    import protovalidate

    mock_stream: AsyncMock = AsyncMock()
    mock_stream.peer = MagicMock()
    mock_stream.peer.addr.return_value = ("127.0.0.1", 54321)
    mock_stream.peer.cert.return_value = None

    ping_envelope = pb.CloudEnvelope(protocol_version=2, device_id="DEV_001")

    async def async_iter():
        yield ping_envelope

    def _aiter(self: object):
        return async_iter()

    mock_stream.__aiter__ = _aiter

    with patch("protovalidate.validate", side_effect=protovalidate.ValidationError("invalid", violations=[])):
        await cloud_service.Session(mock_stream)
        assert not mock_stream.send_message.called


def test_gateway_main_block_simulation() -> None:
    import runpy
    import sys

    with patch.object(sys, "argv", ["gateway.py", "--help"]):
        with pytest.raises(SystemExit):
            runpy.run_path("mcubridge-gateway/gateway.py", run_name="__main__")
