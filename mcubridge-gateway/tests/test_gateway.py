"""Unit tests for the mcubridge-gateway gRPC server and CLI interface."""

from __future__ import annotations

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

    mock_stream.__aiter__ = lambda self: async_iter()

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

    mock_stream.__aiter__ = lambda self: async_iter()

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

    mock_stream.__aiter__ = lambda self: async_iter()

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
