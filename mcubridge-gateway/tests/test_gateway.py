"""Unit tests for the mcubridge-gateway gRPC server and CLI interface."""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any, cast
from unittest.mock import AsyncMock, MagicMock

import pytest
from gateway import (
    CloudBridgeService,
    FleetMetrics,
    GatewayLocalBridgeService,
    GatewaySessionMachine,
    GatewaySessionState,
    ProtobufGateway,
    TSDBSink,
    app,
    auth_interceptor,
    extract_peer_identity,
)
from collections.abc import Callable
from grpclib.const import Status
from grpclib.exceptions import GRPCError
from grpclib.reflection.service import ServerReflection
from hypothesis import given, settings
from hypothesis import strategies as st
import hypothesis.stateful as h_stateful
from hypothesis.stateful import RuleBasedStateMachine, invariant, rule
from mcubridge.protocol import mcubridge_pb2 as pb
from mcubridge.protocol.protocol import DEFAULT_CLOUD_PORT
from pytest_mock import MockerFixture
from typer.testing import CliRunner

_RUN_STATE_MACHINE: Callable[[type[RuleBasedStateMachine]], None] = cast(
    Callable[[type[RuleBasedStateMachine]], None],
    getattr(h_stateful, "run_state_machine_as_test"),
)


@pytest.fixture
def mock_gateway() -> ProtobufGateway:
    return ProtobufGateway(host="127.0.0.1", port=DEFAULT_CLOUD_PORT, use_tls=False)


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


def test_protobuf_gateway_ssl_context_valid(tmp_path: Path, mocker: MockerFixture) -> None:
    cert_file = tmp_path / "server.crt"
    key_file = tmp_path / "server.key"
    cert_file.write_text("dummy cert")
    key_file.write_text("dummy key")

    gw = ProtobufGateway(use_tls=True, cert_file=str(cert_file), key_file=str(key_file))
    mock_ssl_ctx = mocker.patch("ssl.create_default_context")
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


def test_protobuf_gateway_mtls(tmp_path: Path, mocker: MockerFixture) -> None:
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
    mock_ssl_ctx = mocker.patch("ssl.create_default_context")
    mock_ctx = MagicMock()
    mock_ssl_ctx.return_value = mock_ctx
    ctx = gw.get_ssl_context()
    assert ctx is mock_ctx
    assert mock_ctx.load_verify_locations.called


@pytest.mark.asyncio
async def test_protobuf_gateway_run(mocker: MockerFixture) -> None:
    gw = ProtobufGateway(use_tls=False)
    mock_server_cls = mocker.patch("gateway.Server")
    mock_server = AsyncMock()
    mock_server.__dispatch__ = MagicMock()
    mock_server_cls.return_value = mock_server
    await gw.run()
    assert mock_server.start.called
    assert mock_server.wait_closed.called


def test_cli_main_invocation(mocker: MockerFixture) -> None:
    runner = CliRunner()

    mock_runner_instance = MagicMock()

    def _mock_run(coro: Any) -> None:
        if hasattr(coro, "close"):
            coro.close()

    mock_runner_instance.run = _mock_run
    mock_runner_instance.__enter__ = MagicMock(return_value=mock_runner_instance)
    mock_runner_instance.__exit__ = MagicMock(return_value=False)

    mocker.patch("asyncio.Runner", return_value=mock_runner_instance)
    result = runner.invoke(cast(Any, app), ["--no-tls", "--port", "9090"])
    assert result.exit_code == 0


def test_cli_main_keyboard_interrupt(mocker: MockerFixture) -> None:
    runner = CliRunner()

    mock_runner_instance = MagicMock()

    def _mock_run_interrupt(coro: Any) -> None:
        if hasattr(coro, "close"):
            coro.close()
        raise KeyboardInterrupt

    mock_runner_instance.run = _mock_run_interrupt
    mock_runner_instance.__enter__ = MagicMock(return_value=mock_runner_instance)
    mock_runner_instance.__exit__ = MagicMock(return_value=False)

    mock_logger_info = MagicMock()
    mocker.patch("asyncio.Runner", return_value=mock_runner_instance)
    mocker.patch("gateway.logger.info", mock_logger_info)
    result = runner.invoke(cast(Any, app), ["--no-tls", "--http3"])
    assert result.exit_code == 0
    mock_logger_info.assert_called_once_with("Gateway terminated by user.")


@pytest.mark.asyncio
async def test_protobuf_gateway_http3_run(mocker: MockerFixture) -> None:
    gw = ProtobufGateway(use_tls=False, http3_enabled=True, http3_port=9999)
    mock_server_cls = mocker.patch("gateway.Server")
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


def test_gateway_main_block_simulation(mocker: MockerFixture) -> None:
    import runpy
    import sys
    from pathlib import Path

    gateway_path = str(Path(__file__).resolve().parent.parent / "gateway.py")
    mocker.patch.object(sys, "argv", ["gateway.py", "--help"])
    with pytest.raises(SystemExit):
        runpy.run_path(gateway_path, run_name="__main__")


class GatewaySessionStateMachine(RuleBasedStateMachine):
    """Formal SIL-2 property verification for GatewaySessionMachine invariants."""

    def __init__(self) -> None:
        super().__init__()
        self.fsm = GatewaySessionMachine()

    @rule()
    def authenticate(self) -> None:
        prev = self.fsm.current_state_value
        self.fsm.authenticate()
        if prev == GatewaySessionState.CONNECTED.value:
            assert self.fsm.authenticated.is_active
            assert self.fsm.current_state_value == GatewaySessionState.AUTHENTICATED.value
        else:
            assert self.fsm.current_state_value == prev

    @rule()
    def activate(self) -> None:
        prev = self.fsm.current_state_value
        self.fsm.activate()
        if prev in (GatewaySessionState.CONNECTED.value, GatewaySessionState.AUTHENTICATED.value):
            assert self.fsm.active.is_active
            assert self.fsm.current_state_value == GatewaySessionState.ACTIVE.value
        else:
            assert self.fsm.current_state_value == prev

    @rule()
    def close(self) -> None:
        self.fsm.close()
        assert self.fsm.closed.is_active
        assert self.fsm.current_state_value == GatewaySessionState.CLOSED.value

    @invariant()
    def valid_state(self) -> None:
        assert self.fsm.current_state_value in (
            GatewaySessionState.CONNECTED.value,
            GatewaySessionState.AUTHENTICATED.value,
            GatewaySessionState.ACTIVE.value,
            GatewaySessionState.CLOSED.value,
        )


def test_gateway_session_machine_lifecycle() -> None:
    """Execute Hypothesis RuleBasedStateMachine on GatewaySessionMachine."""
    _RUN_STATE_MACHINE(GatewaySessionStateMachine)


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

    # 6. metadata with x-device-id
    device_id_meta, auth_meta = extract_peer_identity(mock_peer, {"x-device-id": "yun-meta-01"})
    assert device_id_meta == "yun-meta-01"
    assert auth_meta is False


@pytest.mark.asyncio
async def test_auth_interceptor_flow() -> None:
    # Valid call flow through interceptor
    invocations: list[Any] = []

    async def dummy_handler(stream: Any) -> None:
        invocations.append(stream)

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
    assert invocations == [mock_stream]

    # Error handling when cert is invalid
    mock_event_bad = MagicMock()
    mock_event_bad.method_func = dummy_handler
    mock_event_bad.peer = MagicMock()
    mock_event_bad.peer.addr.return_value = ("10.0.0.5", 8080)
    mock_event_bad.peer.cert.return_value = {"subject": [None]}

    await auth_interceptor(mock_event_bad)
    # When cert is invalid, interceptor does not wrap and returns early
    assert mock_event_bad.method_func == dummy_handler


@pytest.mark.asyncio
async def test_fleet_metrics_and_telemetry_flow(cloud_service: CloudBridgeService) -> None:
    mock_stream: AsyncMock = AsyncMock()
    mock_stream.peer = MagicMock()
    mock_stream.peer.addr.return_value = ("10.0.0.2", 12345)
    mock_stream.peer.cert.return_value = {"subject": [[("commonName", "test-yun-01")]]}

    metrics = pb.DaemonMetrics(
        cloud_queue_depth=4,
        link_synchronised=True,
        cloud_spool_pending_messages=2,
        watchdog_enabled=True,
    )
    telemetry_envelope = pb.CloudEnvelope(
        protocol_version=2,
        device_id="test-yun-01",
        sequence_id=1,
        telemetry=pb.TelemetryReport(daemon_metrics_blob=metrics.SerializeToString()),
    )
    event_envelope = pb.CloudEnvelope(
        protocol_version=2,
        device_id="test-yun-01",
        sequence_id=2,
        event=pb.EventNotification(event_type="alarm", severity="warning", description="temp high"),
    )

    async def async_iter():
        yield telemetry_envelope
        yield event_envelope

    def _aiter(self: object):
        return async_iter()

    mock_stream.__aiter__ = _aiter

    await cloud_service.Session(mock_stream)

    gw = cloud_service.gateway
    assert isinstance(gw.metrics, FleetMetrics)
    assert gw.metrics.registry.get_sample_value("mcubridge_device_connected", {"device_id": "test-yun-01"}) == 0.0
    assert gw.metrics.registry.get_sample_value("mcubridge_device_telemetry_total", {"device_id": "test-yun-01"}) == 1.0
    events_val = gw.metrics.registry.get_sample_value(
        "mcubridge_device_events_total", {"device_id": "test-yun-01", "severity": "warning"}
    )
    assert events_val == 1.0
    assert (
        gw.metrics.registry.get_sample_value("mcubridge_device_link_synchronized", {"device_id": "test-yun-01"}) == 1.0
    )
    assert (
        gw.metrics.registry.get_sample_value("mcubridge_device_cloud_queue_depth", {"device_id": "test-yun-01"}) == 4.0
    )
    spool_val = gw.metrics.registry.get_sample_value(
        "mcubridge_device_spool_pending_messages", {"device_id": "test-yun-01"}
    )
    assert spool_val == 2.0
    assert (
        gw.metrics.registry.get_sample_value("mcubridge_device_watchdog_enabled", {"device_id": "test-yun-01"}) == 1.0
    )


def test_tsdb_sink_formatting_and_ingestion() -> None:

    sink = TSDBSink(endpoint_url="http://localhost:8428/write")
    assert sink.enabled is True

    metrics = pb.DaemonMetrics(
        cloud_queue_depth=5,
        cloud_dropped_messages=1,
        cloud_spool_pending_messages=3,
        cloud_spool_degraded=False,
        link_synchronised=True,
        watchdog_enabled=True,
    )
    line = sink.format_line_protocol("yun-node-1", metrics, timestamp_ns=1700000000000000000)
    assert "mcubridge_telemetry,device_id=yun-node-1" in line
    assert "queue_depth=5i" in line
    assert "link_synchronized=1i" in line
    assert "watchdog_enabled=1i" in line
    assert line.endswith("1700000000000000000")


@pytest.mark.asyncio
async def test_tsdb_sink_async_post_mocked(mocker: MockerFixture) -> None:
    sink = TSDBSink(endpoint_url="http://localhost:8428/write")
    metrics = pb.DaemonMetrics(cloud_queue_depth=1)
    envelope = pb.CloudEnvelope(
        protocol_version=2,
        telemetry=pb.TelemetryReport(daemon_metrics_blob=metrics.SerializeToString()),
    )

    mock_open = mocker.patch("urllib.request.OpenerDirector.open")
    mock_resp = MagicMock()
    mock_resp.status = 204
    mock_resp.__enter__.return_value = mock_resp
    mock_resp.__exit__.return_value = False
    mock_open.return_value = mock_resp

    spy_format = mocker.spy(sink, "format_line_protocol")
    await sink.ingest_telemetry("yun-node-1", envelope)
    assert mock_open.called
    assert spy_format.call_count == 1


@pytest.mark.asyncio
async def test_send_command_success_and_correlation(mock_gateway: ProtobufGateway) -> None:
    mock_stream = AsyncMock()
    mock_gateway.connections["device-test"] = mock_stream

    async def _send_and_respond():
        await asyncio.sleep(0.01)
        assert len(mock_gateway.pending_commands) == 1
        key = list(mock_gateway.pending_commands.keys())[0]
        seq = key[1]
        resp_envelope = pb.CloudEnvelope(
            protocol_version=2,
            device_id="device-test",
            sequence_id=seq,
            command_response=pb.CommandResponse(status_code=200, payload=b"OK"),
        )
        mock_gateway.handle_command_response("device-test", seq, resp_envelope.command_response)

    task = asyncio.create_task(_send_and_respond())
    resp = await mock_gateway.send_command("device-test", "/gpio/write", payload=b"\x01")
    await task

    assert resp.status_code == 200
    assert resp.payload == b"OK"
    assert len(mock_gateway.pending_commands) == 0


@pytest.mark.asyncio
async def test_send_command_device_not_connected(mock_gateway: ProtobufGateway) -> None:
    with pytest.raises(KeyError, match="Device non-existent is not connected"):
        await mock_gateway.send_command("non-existent", "/status")


@pytest.mark.asyncio
async def test_send_command_device_disconnected_during_execution(mock_gateway: ProtobufGateway) -> None:
    mock_stream = AsyncMock()
    mock_gateway.connections["device-abort"] = mock_stream

    async def _disconnect_device():
        await asyncio.sleep(0.01)
        for (d_id, _), fut in list(mock_gateway.pending_commands.items()):
            if d_id == "device-abort":
                fut.set_exception(ConnectionResetError("Device disconnected"))

    task = asyncio.create_task(_disconnect_device())
    with pytest.raises(ConnectionResetError, match="Device disconnected"):
        await mock_gateway.send_command("device-abort", "/ping")
    await task


@pytest.mark.asyncio
async def test_protobuf_gateway_metrics_port_run(mocker: MockerFixture) -> None:
    gw = ProtobufGateway(use_tls=False, metrics_port=9100)
    mock_metrics_server = mocker.patch("prometheus_client.start_http_server")
    mock_server_cls = mocker.patch("gateway.Server")
    mock_server = AsyncMock()
    mock_server.__dispatch__ = MagicMock()
    mock_server_cls.return_value = mock_server
    await gw.run()
    assert mock_metrics_server.called


def test_tsdb_sink_post_line_edge_paths(mocker: MockerFixture) -> None:
    import urllib.error

    # 1. Empty endpoint returns early
    sink_empty = TSDBSink(endpoint_url=None)
    assert not sink_empty.enabled
    post_empty = getattr(sink_empty, "_post_line")
    post_empty("mcu,device=dev1 value=1")

    sink = TSDBSink(endpoint_url="http://localhost:8428/write")
    assert sink.enabled
    post_fn = getattr(sink, "_post_line")

    # 2. Status >= 400
    mock_resp = MagicMock()
    mock_resp.status = 500
    mock_resp.__enter__.return_value = mock_resp
    mock_open = mocker.patch("urllib.request.OpenerDirector.open", return_value=mock_resp)
    post_fn("mcu,device=dev1 value=1")
    assert mock_open.call_count == 1

    # 3. URLError network failure (tenacity retries 2 attempts total)
    mock_open.side_effect = urllib.error.URLError("Refused")
    post_fn("mcu,device=dev1 value=1")
    assert mock_open.call_count == 3


@pytest.mark.asyncio
async def test_tsdb_sink_ingest_telemetry_edge_paths(mocker: MockerFixture) -> None:
    # 1. Disabled sink returns early
    sink_disabled = TSDBSink(endpoint_url=None)
    assert not sink_disabled.enabled
    envelope = pb.CloudEnvelope(telemetry=pb.TelemetryReport(daemon_metrics_blob=b"data"))
    await sink_disabled.ingest_telemetry("dev1", envelope)

    # 2. Corrupted metrics blob caught and logged
    sink = TSDBSink(endpoint_url="http://localhost:8428/write")
    assert sink.enabled
    mock_post = mocker.patch.object(sink, "_post_line")
    envelope_corrupt = pb.CloudEnvelope(telemetry=pb.TelemetryReport(daemon_metrics_blob=b"\xff\xff\xff"))
    await sink.ingest_telemetry("dev1", envelope_corrupt)
    mock_post.assert_not_called()


@pytest.mark.asyncio
async def test_handle_telemetry_edge_paths(mock_gateway: ProtobufGateway, mocker: MockerFixture) -> None:
    import gateway

    handle_telemetry = getattr(gateway, "_handle_telemetry")
    mock_gateway.tsdb_sink = TSDBSink(endpoint_url="http://localhost:8428/write")
    svc = CloudBridgeService(mock_gateway)
    mock_stream = AsyncMock()

    # 1. Empty metrics blob
    raw_metric = mock_gateway.metrics.registry.get_sample_value(
        "mcubridge_device_telemetry_total", {"device_id": "edge-1"}
    )
    init_val = raw_metric or 0.0
    envelope_empty = pb.CloudEnvelope(
        protocol_version=2,
        device_id="edge-1",
        telemetry=pb.TelemetryReport(daemon_metrics_blob=b""),
    )
    await handle_telemetry(svc, "edge-1", mock_stream, envelope_empty)
    telemetry_val1 = mock_gateway.metrics.registry.get_sample_value(
        "mcubridge_device_telemetry_total", {"device_id": "edge-1"}
    )
    assert telemetry_val1 == init_val + 1.0

    # 2. Corrupted metrics blob
    envelope_corrupt = pb.CloudEnvelope(
        protocol_version=2,
        device_id="edge-1",
        telemetry=pb.TelemetryReport(daemon_metrics_blob=b"\xff\xff\xff"),
    )
    mocker.patch("urllib.request.OpenerDirector.open")
    await handle_telemetry(svc, "edge-1", mock_stream, envelope_corrupt)
    telemetry_val2 = mock_gateway.metrics.registry.get_sample_value(
        "mcubridge_device_telemetry_total", {"device_id": "edge-1"}
    )
    assert telemetry_val2 == init_val + 2.0


@pytest.mark.asyncio
async def test_handle_command_response_edge_paths(mock_gateway: ProtobufGateway) -> None:

    # 1. Key not in pending commands (ignored safely)
    resp = pb.CommandResponse(status_code=0, payload=b"ok")
    mock_gateway.handle_command_response("dev-none", 999, resp)

    # 2. Future already done (does not raise InvalidStateError)
    loop = asyncio.get_running_loop()
    fut = loop.create_future()
    fut.set_result(pb.CommandResponse(status_code=1, payload=b"already_done"))
    mock_gateway.pending_commands[("dev-done", 1)] = fut
    mock_gateway.handle_command_response("dev-done", 1, resp)
    assert fut.result().payload == b"already_done"


@pytest.mark.asyncio
async def test_session_disconnect_aborts_pending_commands_with_edge_branches(
    mock_gateway: ProtobufGateway, mocker: MockerFixture
) -> None:
    svc = CloudBridgeService(mock_gateway)
    loop = asyncio.get_running_loop()

    # Create mixed futures in pending_commands
    fut_target = loop.create_future()
    fut_done = loop.create_future()
    fut_done.set_result(pb.CommandResponse(status_code=0, payload=b"done"))
    fut_other = loop.create_future()

    mock_gateway.pending_commands[("disc-dev", 1)] = fut_target
    mock_gateway.pending_commands[("disc-dev", 2)] = fut_done
    mock_gateway.pending_commands[("other-dev", 3)] = fut_other

    # Mock stream raising CancelledError immediately
    mock_stream = AsyncMock()
    mock_stream.__aiter__.side_effect = asyncio.CancelledError()

    mocker.patch("gateway.extract_peer_identity", return_value=("disc-dev", True))
    with pytest.raises(asyncio.CancelledError):
        await svc.Session(mock_stream)

    assert fut_target.done()
    assert isinstance(fut_target.exception(), ConnectionResetError)
    assert not fut_other.done()


@pytest.mark.asyncio
async def test_handle_telemetry_full_metrics_dimensions(mock_gateway: ProtobufGateway) -> None:
    import gateway

    handle_telemetry = getattr(gateway, "_handle_telemetry")
    svc = CloudBridgeService(mock_gateway)
    mock_stream = AsyncMock()

    metrics = pb.DaemonMetrics(
        cloud_queue_depth=5,
        cloud_dropped_messages=2,
        cloud_spool_pending_messages=3,
        link_synchronised=True,
        watchdog_enabled=True,
        serial_bytes_sent=1024,
        serial_bytes_received=2048,
        serial_frames_sent=10,
        serial_frames_received=20,
        serial_crc_errors=1,
        serial_decode_errors=0,
        handshake_attempts=3,
        handshake_successes=2,
        watchdog_beats=50,
        uptime_seconds=120.5,
        cloud_messages_published=8,
        serial_latency_ms=12.5,
        rpc_latency_ms=25.0,
        retries=[pb.ComponentRetry(component="cloud_connect", count=4)],
    )

    envelope = pb.CloudEnvelope(
        protocol_version=2,
        device_id="edge-full",
        telemetry=pb.TelemetryReport(daemon_metrics_blob=metrics.SerializeToString()),
    )

    await handle_telemetry(svc, "edge-full", mock_stream, envelope)

    # Verify all prometheus dimensions are populated in FleetMetrics
    gw_metrics = mock_gateway.metrics
    reg = gw_metrics.registry
    lbl = {"device_id": "edge-full"}
    assert reg.get_sample_value("mcubridge_device_link_synchronized", lbl) == 1.0
    assert reg.get_sample_value("mcubridge_device_serial_bytes_sent", lbl) == 1024.0
    assert reg.get_sample_value("mcubridge_device_serial_bytes_received", lbl) == 2048.0
    assert reg.get_sample_value("mcubridge_device_serial_frames_sent", lbl) == 10.0
    assert reg.get_sample_value("mcubridge_device_serial_frames_received", lbl) == 20.0
    assert reg.get_sample_value("mcubridge_device_serial_crc_errors", lbl) == 1.0
    assert reg.get_sample_value("mcubridge_device_handshake_attempts", lbl) == 3.0
    assert reg.get_sample_value("mcubridge_device_handshake_successes", lbl) == 2.0
    assert reg.get_sample_value("mcubridge_device_watchdog_beats", lbl) == 50.0
    assert reg.get_sample_value("mcubridge_device_uptime_seconds", lbl) == 120.5
    assert reg.get_sample_value("mcubridge_device_cloud_messages_published", lbl) == 8.0
    assert reg.get_sample_value("mcubridge_device_latency_ms", {"device_id": "edge-full", "type": "serial"}) == 12.5
    assert reg.get_sample_value("mcubridge_device_latency_ms", {"device_id": "edge-full", "type": "rpc"}) == 25.0
    retries_lbl = {"device_id": "edge-full", "component": "cloud_connect"}
    assert reg.get_sample_value("mcubridge_device_retries", retries_lbl) == 4.0

    # Verify line protocol format contains all dimensions
    line = TSDBSink.format_line_protocol("edge-full", metrics, timestamp_ns=1700000000000)
    assert "serial_bytes_sent=1024i" in line
    assert "serial_bytes_received=2048i" in line
    assert "serial_crc_errors=1i" in line
    assert "watchdog_beats=50i" in line
    assert "published_messages=8i" in line


@settings(max_examples=30, derandomize=True, deadline=None)
@given(
    device_id=st.text(alphabet="abcdefghijklmnopqrstuvwxyz0123456789_-", min_size=1, max_size=32),
    serial_sent=st.integers(0, 10_000_000),
    serial_recv=st.integers(0, 10_000_000),
    crc_errors=st.integers(0, 100_000),
    beats=st.integers(0, 100_000),
    published=st.integers(0, 100_000),
    sync=st.booleans(),
    spool_deg=st.booleans(),
    watchdog=st.booleans(),
    ts=st.integers(1_000_000_000, 2_000_000_000_000),
)
def test_tsdb_sink_format_line_protocol_property(
    device_id: str,
    serial_sent: int,
    serial_recv: int,
    crc_errors: int,
    beats: int,
    published: int,
    sync: bool,
    spool_deg: bool,
    watchdog: bool,
    ts: int,
) -> None:
    metrics = pb.DaemonMetrics(
        serial_bytes_sent=serial_sent,
        serial_bytes_received=serial_recv,
        serial_crc_errors=crc_errors,
        watchdog_beats=beats,
        cloud_messages_published=published,
        link_synchronised=sync,
        cloud_spool_degraded=spool_deg,
        watchdog_enabled=watchdog,
    )
    line = TSDBSink.format_line_protocol(device_id, metrics, timestamp_ns=ts)
    assert line.startswith(f"mcubridge_telemetry,device_id={device_id} ")
    assert line.endswith(f" {ts}")
    assert f"serial_bytes_sent={serial_sent}i" in line
    assert f"serial_bytes_received={serial_recv}i" in line
    assert f"serial_crc_errors={crc_errors}i" in line
    assert f"watchdog_beats={beats}i" in line
    assert f"published_messages={published}i" in line
    assert f"link_synchronized={1 if sync else 0}i" in line
    assert f"spool_degraded={1 if spool_deg else 0}i" in line
    assert f"watchdog_enabled={1 if watchdog else 0}i" in line


@pytest.mark.parametrize(
    ("dispatch_msg", "connected_devs", "send_result", "expected_code", "expected_payload_substr"),
    [
        (None, set[str](), None, None, None),
        (
            pb.CommandDispatch(target_device_id="", command_path="rpc/DigitalWrite"),
            set[str](),
            None,
            400,
            b"Explicit target_device_id is required",
        ),
        (
            pb.CommandDispatch(target_device_id="dev-unknown", command_path="rpc/DigitalWrite"),
            set[str](),
            None,
            503,
            b"is not connected",
        ),
        (
            pb.CommandDispatch(
                target_device_id="dev-1",
                command_path="rpc/DigitalWrite",
                payload=pb.DigitalWrite(pin=13, value=1).SerializeToString(),
            ),
            {"dev-1"},
            pb.CommandResponse(status_code=200, payload=pb.GenericResponse(status="ok").SerializeToString()),
            200,
            None,
        ),
        (
            pb.CommandDispatch(
                target_device_id="dev-1",
                command_path="rpc/AnalogWrite",
                payload=pb.AnalogWrite(pin=5, value=128).SerializeToString(),
            ),
            {"dev-1"},
            pb.CommandResponse(status_code=200, payload=pb.GenericResponse(status="ok").SerializeToString()),
            200,
            None,
        ),
        (
            pb.CommandDispatch(target_device_id="dev-1", command_path="rpc/DigitalWrite", timeout_seconds=2),
            {"dev-1"},
            TimeoutError("Device response timeout"),
            504,
            b"Device response timeout",
        ),
    ],
)
@pytest.mark.asyncio
async def test_dispatch_command_branches(
    mock_gateway: ProtobufGateway,
    dispatch_msg: pb.CommandDispatch | None,
    connected_devs: set[str],
    send_result: pb.CommandResponse | Exception | None,
    expected_code: int | None,
    expected_payload_substr: bytes | None,
) -> None:
    svc = CloudBridgeService(mock_gateway)
    mock_gateway.connections.clear()
    for dev in connected_devs:
        mock_gateway.connections[dev] = AsyncMock()

    if isinstance(send_result, Exception):
        setattr(mock_gateway, "send_command", AsyncMock(side_effect=send_result))
    elif send_result is not None:
        setattr(mock_gateway, "send_command", AsyncMock(return_value=send_result))

    stream = AsyncMock()
    stream.recv_message = AsyncMock(return_value=dispatch_msg)
    await svc.DispatchCommand(stream)

    if expected_code is None:
        stream.send_message.assert_not_called()
    else:
        stream.send_message.assert_called_once()
        resp = stream.send_message.call_args[0][0]
        assert resp.status_code == expected_code
        if expected_payload_substr is not None:
            assert expected_payload_substr in resp.payload


@pytest.mark.asyncio
async def test_gateway_local_bridge_service_dispatch(mock_gateway: ProtobufGateway) -> None:
    local_svc = GatewayLocalBridgeService(mock_gateway)

    # 1. Missing explicit device_id -> raises GRPCError INVALID_ARGUMENT
    stream_no_dev = AsyncMock()
    stream_no_dev.metadata = {}
    stream_no_dev.recv_message = AsyncMock(return_value=pb.DigitalWrite(pin=13, value=1))
    with pytest.raises(GRPCError) as exc_info:
        await local_svc.DigitalWrite(stream_no_dev)
    assert exc_info.value.status == Status.INVALID_ARGUMENT
    assert "Explicit device resolution required" in str(exc_info.value.message)

    # 2. Device not connected -> raises GRPCError UNAVAILABLE
    stream_unknown_dev = AsyncMock()
    stream_unknown_dev.metadata = {"x-device-id": "dev-missing"}
    stream_unknown_dev.recv_message = AsyncMock(return_value=pb.DigitalWrite(pin=13, value=1))
    with pytest.raises(GRPCError) as exc_info_unavail:
        await local_svc.DigitalWrite(stream_unknown_dev)
    assert exc_info_unavail.value.status == Status.UNAVAILABLE
    assert "Explicit target device 'dev-missing' is not connected" in str(exc_info_unavail.value.message)

    # 3. Connected device -> forwards command and returns response
    mock_gateway.connections["dev-1"] = AsyncMock()
    mock_send = AsyncMock(
        return_value=pb.CommandResponse(
            status_code=200,
            payload=pb.GenericResponse(status="ok").SerializeToString(),
        )
    )
    setattr(mock_gateway, "send_command", mock_send)
    stream_valid = AsyncMock()
    stream_valid.metadata = {"x-device-id": "dev-1"}
    stream_valid.recv_message = AsyncMock(return_value=pb.DigitalWrite(pin=13, value=1))
    await local_svc.DigitalWrite(stream_valid)
    mock_send.assert_awaited_once()
    assert stream_valid.send_message.call_args[0][0].status == "ok"

    # 4. SubscribeConsole explicit device resolution
    stream_sub_no_dev = AsyncMock()
    stream_sub_no_dev.metadata = {}
    stream_sub_no_dev.recv_message = AsyncMock(return_value=pb.SubscribeRequest())
    with pytest.raises(GRPCError) as sub_exc_info:
        await local_svc.SubscribeConsole(stream_sub_no_dev)
    assert sub_exc_info.value.status == Status.INVALID_ARGUMENT

    stream_sub_unknown = AsyncMock()
    stream_sub_unknown.metadata = {"x-device-id": "dev-missing"}
    stream_sub_unknown.recv_message = AsyncMock(return_value=pb.SubscribeRequest())
    with pytest.raises(GRPCError) as sub_unavail:
        await local_svc.SubscribeConsole(stream_sub_unknown)
    assert sub_unavail.value.status == Status.UNAVAILABLE

    # 5. Metadata resolution edge cases
    stream_empty_meta = AsyncMock()
    stream_empty_meta.metadata = None
    assert local_svc.resolve_device_id(stream_empty_meta) is None

    stream_empty_list = AsyncMock()
    stream_empty_list.metadata = {"x-device-id": []}
    assert local_svc.resolve_device_id(stream_empty_list) is None

    stream_bytes_meta = AsyncMock()
    stream_bytes_meta.metadata = {"device-id": b"dev-bytes"}
    assert local_svc.resolve_device_id(stream_bytes_meta) == "dev-bytes"

    stream_str_meta = AsyncMock()
    stream_str_meta.metadata = {"device_id": "dev-str"}
    assert local_svc.resolve_device_id(stream_str_meta) == "dev-str"

    stream_list_bytes = AsyncMock()
    stream_list_bytes.metadata = {"x-device-id": [b"dev-first"]}
    assert local_svc.resolve_device_id(stream_list_bytes) == "dev-first"

    # 6. Stream returns None (client disconnect before sending request)
    stream_none = AsyncMock()
    stream_none.metadata = {"x-device-id": "dev-1"}
    stream_none.recv_message = AsyncMock(return_value=None)
    await local_svc.DigitalWrite(stream_none)
    stream_none.send_message.assert_not_called()

    # 7. Error forwarding branches (non-200 status code and TimeoutError)
    stream_err = AsyncMock()
    stream_err.metadata = {"x-device-id": "dev-1"}
    stream_err.recv_message = AsyncMock(return_value=pb.DigitalWrite(pin=13, value=1))
    mock_send_err = AsyncMock(return_value=pb.CommandResponse(status_code=500, error_message="fail"))
    setattr(mock_gateway, "send_command", mock_send_err)
    await local_svc.DigitalWrite(stream_err)
    assert stream_err.send_message.call_args[0][0].status == "error"

    mock_send_timeout = AsyncMock(side_effect=TimeoutError("timed out"))
    setattr(mock_gateway, "send_command", mock_send_timeout)
    await local_svc.DigitalWrite(stream_err)
    assert stream_err.send_message.call_args[0][0].status == "error"

    # 8. Exhaustive invocation of all LocalBridge RPC methods on connected device
    rpc_cases = [
        (local_svc.SetPinMode, pb.PinMode(pin=1, mode=pb.PinModeType.PIN_OUTPUT), pb.GenericResponse(status="ok")),
        (local_svc.DigitalRead, pb.PinRead(pin=13), pb.DigitalReadResponse(value=1)),
        (local_svc.AnalogWrite, pb.AnalogWrite(pin=9, value=128), pb.GenericResponse(status="ok")),
        (local_svc.AnalogRead, pb.PinRead(pin=0), pb.AnalogReadResponse(value=512)),
        (
            local_svc.PinSubscribe,
            pb.PinSubscribeRequest(pin=2, enabled=True),
            pb.PinSubscribeResponse(pin=2, success=True),
        ),
        (local_svc.DatastorePut, pb.DatastorePut(key="k", value=b"v"), pb.GenericResponse(status="ok")),
        (local_svc.DatastoreGet, pb.DatastoreGet(key="k"), pb.DatastoreGetResponse(value=b"v")),
        (local_svc.MailboxPush, pb.MailboxPush(data=b"m"), pb.GenericResponse(status="ok")),
        (local_svc.MailboxRead, pb.SubscribeRequest(), pb.MailboxReadResponse(content=b"m")),
        (local_svc.FileWrite, pb.FileWrite(path="f", data=b"d"), pb.GenericResponse(status="ok")),
        (local_svc.FileRead, pb.FileRead(path="f"), pb.FileReadResponse(content=b"d")),
        (local_svc.FileRemove, pb.FileRemove(path="f"), pb.GenericResponse(status="ok")),
        (local_svc.ProcessRunAsync, pb.ProcessRunAsync(command="echo"), pb.ProcessRunAsyncResponse(pid=123)),
        (local_svc.ProcessPoll, pb.ProcessPoll(pid=123), pb.ProcessPollResponse(status=0, finished=True)),
        (local_svc.ProcessKill, pb.ProcessKill(pid=123), pb.GenericResponse(status="ok")),
        (local_svc.SpiTransfer, pb.SpiTransfer(data=b"x"), pb.SpiTransferResponse(data=b"x")),
        (local_svc.SpiConfigure, pb.SpiConfig(frequency=1000000), pb.GenericResponse(status="ok")),
        (local_svc.GetVersion, pb.SubscribeRequest(), pb.VersionResponse(major=2, minor=0, patch=0)),
        (local_svc.GetFreeMemory, pb.SubscribeRequest(), pb.FreeMemoryResponse(value=1024)),
        (local_svc.GetStatus, pb.SubscribeRequest(), pb.BridgeStatus()),
    ]
    for rpc_fn, req_msg, resp_msg in rpc_cases:
        rpc_stream = AsyncMock()
        rpc_stream.metadata = {"x-device-id": "dev-1"}
        rpc_stream.recv_message = AsyncMock(return_value=req_msg)
        setattr(
            mock_gateway,
            "send_command",
            AsyncMock(return_value=pb.CommandResponse(status_code=200, payload=resp_msg.SerializeToString())),
        )
        await rpc_fn(rpc_stream)
        rpc_stream.send_message.assert_called_once()

    # 9. Publish RPC (console routing and missing device_id)
    stream_pub_no_dev = AsyncMock()
    stream_pub_no_dev.metadata = {}
    stream_pub_no_dev.recv_message = AsyncMock(return_value=pb.CloudQueuedPublish(topic_name="br/test"))
    await local_svc.Publish(stream_pub_no_dev)
    stream_pub_no_dev.send_message.assert_called_once()

    stream_pub_none = AsyncMock()
    stream_pub_none.metadata = {"x-device-id": "dev-1"}
    stream_pub_none.recv_message = AsyncMock(return_value=None)
    await local_svc.Publish(stream_pub_none)

    # Console queue subscription and publishing flow
    console_q: asyncio.Queue[pb.CloudQueuedPublish] = asyncio.Queue()
    mock_gateway.console_queues["dev-1"] = [console_q]
    stream_pub_console = AsyncMock()
    stream_pub_console.metadata = {"x-device-id": "dev-1"}
    pub_console_msg = pb.CloudQueuedPublish(topic_name="br/console/write", payload=b"ping")
    stream_pub_console.recv_message = AsyncMock(return_value=pub_console_msg)
    setattr(
        mock_gateway,
        "send_command",
        AsyncMock(return_value=pb.CommandResponse(status_code=200, payload=pub_console_msg.SerializeToString())),
    )
    await local_svc.Publish(stream_pub_console)
    assert not console_q.empty()
    queued_item = console_q.get_nowait()
    assert queued_item.topic_name == "br/console/write"

    # 10. SubscribeConsole active delivery and termination
    stream_sub_active = AsyncMock()
    stream_sub_active.metadata = {"x-device-id": "dev-1"}
    stream_sub_active.recv_message = AsyncMock(return_value=pb.SubscribeRequest())
    stream_sub_active.send_message = AsyncMock(side_effect=RuntimeError("stream disconnect"))

    async def _feed_console() -> None:
        await asyncio.sleep(0.01)
        for q in mock_gateway.console_queues.get("dev-1", []):
            q.put_nowait(pb.CloudQueuedPublish(topic_name="br/console/out", payload=b"hello"))

    asyncio.create_task(_feed_console())
    await local_svc.SubscribeConsole(stream_sub_active)
    stream_sub_active.send_message.assert_awaited_once()

    # SubscribeConsole when recv_message returns None
    stream_sub_none = AsyncMock()
    stream_sub_none.metadata = {"x-device-id": "dev-1"}
    stream_sub_none.recv_message = AsyncMock(return_value=None)
    await local_svc.SubscribeConsole(stream_sub_none)

    # 11. Publish with non-console topic (covers branch 707->710)
    stream_pub_non_console = AsyncMock()
    stream_pub_non_console.metadata = {"x-device-id": "dev-1"}
    pub_data_msg = pb.CloudQueuedPublish(topic_name="br/telemetry/data", payload=b"123")
    stream_pub_non_console.recv_message = AsyncMock(return_value=pub_data_msg)
    setattr(
        mock_gateway,
        "send_command",
        AsyncMock(return_value=pb.CommandResponse(status_code=200, payload=pub_data_msg.SerializeToString())),
    )
    await local_svc.Publish(stream_pub_non_console)

    # 12. SubscribeConsole when device_id removed from console_queues before exit (covers branch 738)
    stream_sub_cleanup = AsyncMock()
    stream_sub_cleanup.metadata = {"x-device-id": "dev-1"}
    stream_sub_cleanup.recv_message = AsyncMock(return_value=pb.SubscribeRequest())

    async def _fail_and_clear_queues(_msg: Any) -> None:
        mock_gateway.console_queues.pop("dev-1", None)
        raise RuntimeError("stream aborted")

    stream_sub_cleanup.send_message = AsyncMock(side_effect=_fail_and_clear_queues)

    async def _feed_console_cleanup() -> None:
        await asyncio.sleep(0.01)
        for q in mock_gateway.console_queues.get("dev-1", []):
            q.put_nowait(pb.CloudQueuedPublish(topic_name="br/console/out", payload=b"bye"))

    asyncio.create_task(_feed_console_cleanup())
    await local_svc.SubscribeConsole(stream_sub_cleanup)


def test_tsdb_sink_invalid_scheme() -> None:
    with pytest.raises(ValueError, match="Invalid TSDB scheme 'file'"):
        TSDBSink(endpoint_url="file:///etc/passwd")
    with pytest.raises(ValueError, match="Invalid TSDB scheme 'ftp'"):
        TSDBSink(endpoint_url="ftp://evil.com/write")


@pytest.mark.asyncio
async def test_protobuf_gateway_reflection_services() -> None:
    gw = ProtobufGateway(use_tls=False)
    services = [CloudBridgeService(gw), GatewayLocalBridgeService(gw)]
    extended = ServerReflection.extend(services)
    assert len(extended) == 4
    service_names = [type(s).__name__ for s in extended]
    assert "ServerReflection" in service_names
