from typing import Any, cast
import asyncio
from pathlib import Path
import sys
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

# Ensure mcubridge-gateway directory is in sys.path
GATEWAY_DIR = Path(__file__).resolve().parent.parent.parent / "mcubridge-gateway"
if str(GATEWAY_DIR) not in sys.path:
    sys.path.insert(0, str(GATEWAY_DIR))

from gateway import CloudBridgeService, ProtobufGateway, main  # type: ignore # noqa: E402
from mcubridge.protocol import mcubridge_pb2 as pb  # noqa: E402


@pytest.mark.asyncio
async def test_gateway_service_session_ping_and_events():
    gateway = ProtobufGateway(use_tls=False)
    service = CloudBridgeService(gateway)

    mock_stream = AsyncMock()
    mock_stream.peer = MagicMock()
    mock_stream.peer.addr.return_value = ("127.0.0.1", 12345)
    mock_stream.peer.cert.return_value = {"subject": ((("commonName", "test-device-01"),),)}

    env_ping = pb.CloudEnvelope(sequence_id=1, ping=pb.KeepalivePing())
    env_telemetry = pb.CloudEnvelope(sequence_id=2, telemetry=pb.TelemetryReport())
    env_event = pb.CloudEnvelope(
        sequence_id=3,
        event=pb.EventNotification(event_type="INFO", description="Test event"),
    )
    env_response = pb.CloudEnvelope(sequence_id=4, command_response=pb.CommandResponse(status_code=200))

    mock_stream.__aiter__.return_value = [
        env_ping,
        env_telemetry,
        env_event,
        env_response,
    ]

    await service.Session(mock_stream)

    assert mock_stream.send_message.call_count == 1
    sent_msg = mock_stream.send_message.call_args[0][0]
    assert sent_msg.sequence_id == 1
    assert sent_msg.HasField("pong")


@pytest.mark.asyncio
async def test_gateway_service_session_cert_error_and_oserror():
    gateway = ProtobufGateway(use_tls=False)
    service = CloudBridgeService(gateway)

    mock_stream = AsyncMock()
    mock_stream.peer = MagicMock()
    mock_stream.peer.addr.return_value = None
    mock_stream.peer.cert.return_value = None

    async def _async_raise_oserror():
        raise OSError("Network error")
        yield  # type: ignore

    mock_stream.__aiter__.side_effect = _async_raise_oserror

    await service.Session(mock_stream)
    assert "anonymous-unknown" not in gateway.connections


@pytest.mark.asyncio
async def test_gateway_service_session_cancelled_error():
    gateway = ProtobufGateway(use_tls=False)
    service = CloudBridgeService(gateway)

    mock_stream = AsyncMock()
    mock_stream.peer = MagicMock()
    mock_stream.peer.addr.return_value = ("10.0.0.1", 54321)
    mock_stream.peer.cert.return_value = None

    async def _async_raise_cancelled():
        raise asyncio.CancelledError()
        yield  # type: ignore

    mock_stream.__aiter__.side_effect = _async_raise_cancelled

    with pytest.raises(asyncio.CancelledError):
        await service.Session(mock_stream)


def test_protobuf_gateway_ssl_context(tmp_path: Path):
    # No TLS
    gw_no_tls = ProtobufGateway(use_tls=False)
    assert cast(Any, gw_no_tls)._get_ssl_context() is None

    # TLS without cert files
    gw_no_cert = ProtobufGateway(use_tls=True)
    assert cast(Any, gw_no_cert)._get_ssl_context() is None

    # TLS with cert files
    cert_file: Path = tmp_path / "cert.pem"
    key_file: Path = tmp_path / "key.pem"
    ca_file: Path = tmp_path / "ca.pem"
    cert_file.write_text("dummy")
    key_file.write_text("dummy")
    ca_file.write_text("dummy")

    gw_tls = ProtobufGateway(
        use_tls=True,
        cert_file=str(cert_file),
        key_file=str(key_file),
        ca_file=str(ca_file),
    )

    with patch("ssl.create_default_context") as mock_ssl_ctx:
        mock_ctx_inst = MagicMock()
        mock_ssl_ctx.return_value = mock_ctx_inst
        ctx = cast(Any, gw_tls)._get_ssl_context()
        assert ctx is mock_ctx_inst
        mock_ctx_inst.load_cert_chain.assert_called_once_with(certfile=str(cert_file), keyfile=str(key_file))
        mock_ctx_inst.load_verify_locations.assert_called_once_with(cafile=str(ca_file))


@pytest.mark.asyncio
async def test_protobuf_gateway_run():
    gw = ProtobufGateway(use_tls=False)
    with patch("gateway.Server") as mock_server_cls:
        mock_server_inst = AsyncMock()
        mock_server_cls.return_value = mock_server_inst
        mock_server_inst.start = AsyncMock()
        mock_server_inst.wait_closed = AsyncMock(side_effect=asyncio.CancelledError)

        with pytest.raises(asyncio.CancelledError):
            await gw.run()


def test_gateway_main():
    with patch("sys.argv", ["gateway.py", "--no-tls", "--host", "127.0.0.1", "--port", "8443"]):
        with patch("gateway.ProtobufGateway.run", new_callable=AsyncMock) as mock_run:
            main()
            mock_run.assert_called_once()
