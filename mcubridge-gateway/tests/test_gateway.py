"""Unit tests for the mcubridge-gateway gRPC server and CLI interface."""

from __future__ import annotations

import asyncio
from collections.abc import Callable
import logging
from pathlib import Path
import re
from typing import Any, cast
from unittest.mock import AsyncMock, MagicMock

from dirty_equals import Contains, HasLen, IsStr
import httpx
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
    main,
)
from grpclib.const import Status
from grpclib.exceptions import GRPCError
from grpclib.reflection.service import ServerReflection
import hypothesis.stateful as h_stateful
from hypothesis import given, settings
from hypothesis import strategies as st
from hypothesis.stateful import RuleBasedStateMachine, invariant, rule
from mcubridge.protocol import mcubridge_pb2 as pb
from mcubridge.protocol.protocol import DEFAULT_CLOUD_PORT, PROTOCOL_VERSION
from pytest_mock import MockerFixture
from typer.testing import CliRunner

from tools.testing.mock_stream import MockGrpcStream, MockPeer

run_state_machine_as_test: Callable[[type[RuleBasedStateMachine]], None] = cast(
    Any, h_stateful
).run_state_machine_as_test


@pytest.fixture
def mock_gateway() -> ProtobufGateway:
    return ProtobufGateway(host="127.0.0.1", port=DEFAULT_CLOUD_PORT, use_tls=False)


@pytest.fixture
def cloud_service(mock_gateway: ProtobufGateway) -> CloudBridgeService:
    return CloudBridgeService(mock_gateway)


async def test_session_ping_pong(cloud_service: CloudBridgeService) -> None:
    ping_envelope = pb.CloudEnvelope(
        protocol_version=PROTOCOL_VERSION,
        device_id="DEV_001",
        sequence_id=10,
        ping=pb.KeepalivePing(interval_ms=1000),
    )
    mock_stream = MockGrpcStream[pb.CloudEnvelope, pb.CloudEnvelope](inbound=[ping_envelope])

    await cloud_service.Session(mock_stream.as_stream())

    assert mock_stream.send_message.called
    sent_pong = mock_stream.sent_message
    assert sent_pong.sequence_id == 10
    assert sent_pong.WhichOneof("payload") == "pong"


async def test_session_client_cert_common_name(cloud_service: CloudBridgeService) -> None:
    inbound = [
        pb.CloudEnvelope(protocol_version=PROTOCOL_VERSION, telemetry=pb.TelemetryReport(daemon_metrics_blob=b"data"))
    ]
    mock_stream = MockGrpcStream[pb.CloudEnvelope, pb.CloudEnvelope](
        inbound=inbound,
        peer_addr=("10.0.0.1", 12345),
        client_cn="device-mips-01",
    )

    await cloud_service.Session(mock_stream.as_stream())
    assert "device-mips-01" not in cloud_service.gateway.connections


async def test_session_event_and_command_response(cloud_service: CloudBridgeService) -> None:
    event_envelope = pb.CloudEnvelope(
        protocol_version=PROTOCOL_VERSION,
        event=pb.EventNotification(event_type="boot", severity="info", description="MCU reset"),
    )
    cmd_resp_envelope = pb.CloudEnvelope(
        protocol_version=PROTOCOL_VERSION,
        command_response=pb.CommandResponse(status_code=200, error_message="", payload=b""),
    )
    mock_stream = MockGrpcStream[pb.CloudEnvelope, pb.CloudEnvelope](
        inbound=[event_envelope, cmd_resp_envelope],
        peer_addr=("127.0.0.1", 9999),
    )

    await cloud_service.Session(mock_stream.as_stream())
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


async def test_session_unhandled_and_oserror(cloud_service: CloudBridgeService) -> None:
    mock_stream = MockGrpcStream[pb.CloudEnvelope, pb.CloudEnvelope](
        inbound=[pb.CloudEnvelope(protocol_version=PROTOCOL_VERSION)],
        peer_addr=("127.0.0.1", 1234),
        aiter_exception=OSError("Connection reset by peer"),
    )
    await cloud_service.Session(mock_stream.as_stream())
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


def test_cli_main_keyboard_interrupt(mocker: MockerFixture, caplog: pytest.LogCaptureFixture) -> None:
    mock_runner_instance = MagicMock()

    def _mock_run_interrupt(coro: Any) -> None:
        if hasattr(coro, "close"):
            coro.close()
        raise KeyboardInterrupt

    mock_runner_instance.run = _mock_run_interrupt
    mock_runner_instance.__enter__ = MagicMock(return_value=mock_runner_instance)
    mock_runner_instance.__exit__ = MagicMock(return_value=False)

    caplog.set_level(logging.INFO)
    mocker.patch("asyncio.Runner", return_value=mock_runner_instance)
    main(no_tls=True, http3=True)
    assert "Gateway terminated by user." in caplog.text


async def test_protobuf_gateway_http3_run(mocker: MockerFixture) -> None:
    gw = ProtobufGateway(use_tls=False, http3_enabled=True, http3_port=9999)
    mock_server_cls = mocker.patch("gateway.Server")
    mock_server = AsyncMock()
    mock_server.__dispatch__ = MagicMock()
    mock_server_cls.return_value = mock_server
    await gw.run()
    assert mock_server.start.called


async def test_cloud_bridge_service_cert_parse_error() -> None:
    gw = ProtobufGateway(use_tls=False)
    service = CloudBridgeService(gw)
    mock_stream = MockGrpcStream[pb.CloudEnvelope, pb.CloudEnvelope](
        peer_addr=("127.0.0.1", 12345), peer_cert={"subject": [None]}
    )
    await service.Session(mock_stream.as_stream())
    assert len(gw.connections) == 0


async def test_cloud_bridge_service_session_cancelled() -> None:
    gw = ProtobufGateway(use_tls=False)
    service = CloudBridgeService(gw)
    mock_stream = MockGrpcStream[pb.CloudEnvelope, pb.CloudEnvelope](
        peer_addr=("127.0.0.1", 12345), aiter_exception=asyncio.CancelledError()
    )
    with pytest.raises(asyncio.CancelledError):
        await service.Session(mock_stream.as_stream())


async def test_session_invalid_envelope_validation(cloud_service: CloudBridgeService) -> None:
    # Invalid envelope with wrong protocol_version (fails native check)
    invalid_envelope = pb.CloudEnvelope(protocol_version=99, device_id="DEV_001")
    mock_stream = MockGrpcStream[pb.CloudEnvelope, pb.CloudEnvelope](inbound=[invalid_envelope])

    await cloud_service.Session(mock_stream.as_stream())
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
    run_state_machine_as_test(GatewaySessionStateMachine)


async def test_gateway_payload_dispatch_empty_or_unhandled(cloud_service: CloudBridgeService) -> None:
    empty_envelope = pb.CloudEnvelope(
        protocol_version=PROTOCOL_VERSION,
        device_id="DEV_EMPTY",
        sequence_id=99,
    )
    mock_stream = MockGrpcStream[pb.CloudEnvelope, pb.CloudEnvelope](
        inbound=[empty_envelope], peer_addr=("127.0.0.1", 55555)
    )

    await cloud_service.Session(mock_stream.as_stream())
    assert not mock_stream.send_message.called


def test_extract_peer_identity() -> None:
    # 1. peer is None
    device_id, auth = extract_peer_identity(None)
    assert device_id == "anonymous-unknown"
    assert not auth

    # 2. peer with address only
    mock_peer = MockPeer(addr=("192.168.1.50", 44321))
    device_id, auth = extract_peer_identity(mock_peer)
    assert device_id == "anonymous-192.168.1.50:44321"
    assert not auth

    # 3. peer with CN certificate
    mock_peer_cn = MockPeer(common_name="test-device-01")
    device_id, auth = extract_peer_identity(mock_peer_cn)
    assert device_id == "test-device-01"
    assert auth

    # 4. peer with malformed cert subject
    mock_peer_bad = MockPeer(cert={"subject": [None]})
    with pytest.raises(ValueError, match="Failed to parse client certificate"):
        extract_peer_identity(mock_peer_bad)

    # 5. peer with valid cert subject but missing commonName
    mock_peer_no_cn = MockPeer(
        addr=("192.168.1.50", 44321),
        cert={
            "subject": [
                [("countryName", "US")],
                [("organizationName", "Acme Inc")],
            ]
        },
    )
    device_id_no_cn, auth_no_cn = extract_peer_identity(mock_peer_no_cn)
    assert device_id_no_cn == "anonymous-192.168.1.50:44321"
    assert auth_no_cn is False

    # 6. metadata with x-device-id
    device_id_meta, auth_meta = extract_peer_identity(mock_peer_no_cn, {"x-device-id": "yun-meta-01"})
    assert device_id_meta == "yun-meta-01"
    assert auth_meta is False


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


async def test_fleet_metrics_and_telemetry_flow(cloud_service: CloudBridgeService) -> None:
    metrics = pb.DaemonMetrics(
        cloud_queue_depth=4,
        link_synchronised=True,
        cloud_spool_pending_messages=2,
        watchdog_enabled=True,
    )
    telemetry_envelope = pb.CloudEnvelope(
        protocol_version=PROTOCOL_VERSION,
        device_id="test-yun-01",
        sequence_id=1,
        telemetry=pb.TelemetryReport(daemon_metrics_blob=metrics.SerializeToString()),
    )
    event_envelope = pb.CloudEnvelope(
        protocol_version=PROTOCOL_VERSION,
        device_id="test-yun-01",
        sequence_id=2,
        event=pb.EventNotification(event_type="alarm", severity="warning", description="temp high"),
    )
    mock_stream = MockGrpcStream[pb.CloudEnvelope, pb.CloudEnvelope](
        inbound=[telemetry_envelope, event_envelope],
        peer_addr=("10.0.0.2", 12345),
        client_cn="test-yun-01",
    )

    await cloud_service.Session(mock_stream.as_stream())

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


async def test_tsdb_sink_async_post_mocked(mocker: MockerFixture) -> None:
    mock_client = AsyncMock(spec=httpx.AsyncClient)
    mock_resp = MagicMock(spec=httpx.Response)
    mock_resp.status_code = 204
    mock_client.post.return_value = mock_resp

    sink = TSDBSink(endpoint_url="http://localhost:8428/write", client=mock_client)
    metrics = pb.DaemonMetrics(cloud_queue_depth=1)
    envelope = pb.CloudEnvelope(
        protocol_version=PROTOCOL_VERSION,
        telemetry=pb.TelemetryReport(daemon_metrics_blob=metrics.SerializeToString()),
    )

    spy_format = mocker.spy(sink, "format_line_protocol")
    await sink.ingest_telemetry("yun-node-1", envelope)
    assert mock_client.post.called
    assert spy_format.call_count == 1


async def test_send_command_success_and_correlation(mock_gateway: ProtobufGateway) -> None:
    mock_stream = AsyncMock()
    mock_gateway.connections["device-test"] = mock_stream

    async def _send_and_respond():
        await asyncio.sleep(0.01)
        assert mock_gateway.pending_commands == HasLen(1)
        key = list(mock_gateway.pending_commands.keys())[0]
        seq = key[1]
        resp_envelope = pb.CloudEnvelope(
            protocol_version=PROTOCOL_VERSION,
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
    assert mock_gateway.pending_commands == HasLen(0)


async def test_send_command_device_not_connected(mock_gateway: ProtobufGateway) -> None:
    with pytest.raises(KeyError, match="Device non-existent is not connected"):
        await mock_gateway.send_command("non-existent", "/status")


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


async def test_protobuf_gateway_metrics_port_run(mocker: MockerFixture) -> None:
    gw = ProtobufGateway(use_tls=False, metrics_port=9100)
    mock_metrics_server = mocker.patch("prometheus_client.start_http_server")
    mock_server_cls = mocker.patch("gateway.Server")
    mock_server = AsyncMock()
    mock_server.__dispatch__ = MagicMock()
    mock_server_cls.return_value = mock_server
    await gw.run()
    assert mock_metrics_server.called


async def test_tsdb_sink_post_line_edge_paths() -> None:
    # 1. Empty endpoint returns early
    sink_empty = TSDBSink(endpoint_url=None)
    assert not sink_empty.enabled
    await sink_empty.post_line("mcu,device=dev1 value=1")

    mock_client = AsyncMock(spec=httpx.AsyncClient)
    sink = TSDBSink(endpoint_url="http://localhost:8428/write", client=mock_client)
    assert sink.enabled

    # 2. Status >= 400
    mock_resp = MagicMock(spec=httpx.Response)
    mock_resp.status_code = 500
    mock_client.post.return_value = mock_resp
    await sink.post_line("mcu,device=dev1 value=1")
    assert mock_client.post.call_count == 1

    # 3. HTTPError network failure (tenacity retries 2 attempts total)
    mock_client.post.reset_mock()
    mock_client.post.side_effect = httpx.ConnectError("Refused")
    await sink.post_line("mcu,device=dev1 value=1")
    assert mock_client.post.call_count == 2


async def test_tsdb_sink_ingest_telemetry_edge_paths() -> None:
    # 1. Disabled sink returns early
    sink_disabled = TSDBSink(endpoint_url=None)
    assert not sink_disabled.enabled
    envelope = pb.CloudEnvelope(telemetry=pb.TelemetryReport(daemon_metrics_blob=b"data"))
    await sink_disabled.ingest_telemetry("dev1", envelope)

    # 2. Corrupted metrics blob caught and logged
    mock_client = AsyncMock(spec=httpx.AsyncClient)
    sink = TSDBSink(endpoint_url="http://localhost:8428/write", client=mock_client)
    assert sink.enabled
    envelope_corrupt = pb.CloudEnvelope(telemetry=pb.TelemetryReport(daemon_metrics_blob=b"\xff\xff\xff"))
    await sink.ingest_telemetry("dev1", envelope_corrupt)
    mock_client.post.assert_not_called()


async def test_handle_telemetry_edge_paths(mock_gateway: ProtobufGateway, mocker: MockerFixture) -> None:
    import gateway

    handle_telemetry = gateway.handle_telemetry
    mock_gateway.tsdb_sink = TSDBSink(endpoint_url="http://localhost:8428/write")
    svc = CloudBridgeService(mock_gateway)
    mock_stream = AsyncMock()

    # 1. Empty metrics blob
    raw_metric = mock_gateway.metrics.registry.get_sample_value(
        "mcubridge_device_telemetry_total", {"device_id": "edge-1"}
    )
    init_val = raw_metric or 0.0
    envelope_empty = pb.CloudEnvelope(
        protocol_version=PROTOCOL_VERSION,
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
        protocol_version=PROTOCOL_VERSION,
        device_id="edge-1",
        telemetry=pb.TelemetryReport(daemon_metrics_blob=b"\xff\xff\xff"),
    )
    mocker.patch.object(mock_gateway.tsdb_sink.client, "post", new_callable=AsyncMock)
    await handle_telemetry(svc, "edge-1", mock_stream, envelope_corrupt)
    telemetry_val2 = mock_gateway.metrics.registry.get_sample_value(
        "mcubridge_device_telemetry_total", {"device_id": "edge-1"}
    )
    assert telemetry_val2 == init_val + 2.0


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
    mock_stream = MockGrpcStream[pb.CloudEnvelope, pb.CloudEnvelope](
        client_cn="disc-dev", aiter_exception=asyncio.CancelledError()
    )

    with pytest.raises(asyncio.CancelledError):
        await svc.Session(mock_stream.as_stream())

    assert fut_target.done()
    assert isinstance(fut_target.exception(), ConnectionResetError)
    assert not fut_other.done()


async def test_handle_telemetry_full_metrics_dimensions(mock_gateway: ProtobufGateway) -> None:
    import gateway

    handle_telemetry = gateway.handle_telemetry
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
        protocol_version=PROTOCOL_VERSION,
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
    assert line == (
        Contains("serial_bytes_sent=1024i")
        & Contains("serial_bytes_received=2048i")
        & Contains("serial_crc_errors=1i")
        & Contains("watchdog_beats=50i")
        & Contains("published_messages=8i")
    )


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
    assert line == (
        IsStr(regex=rf"^mcubridge_telemetry,device_id={re.escape(device_id)} .* {ts}$")
        & Contains(f"serial_bytes_sent={serial_sent}i")
        & Contains(f"serial_bytes_received={serial_recv}i")
        & Contains(f"serial_crc_errors={crc_errors}i")
        & Contains(f"watchdog_beats={beats}i")
        & Contains(f"published_messages={published}i")
        & Contains(f"link_synchronized={1 if sync else 0}i")
        & Contains(f"spool_degraded={1 if spool_deg else 0}i")
        & Contains(f"watchdog_enabled={1 if watchdog else 0}i")
    )


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
        mock_gateway.send_command = AsyncMock(side_effect=send_result)
    elif send_result is not None:
        mock_gateway.send_command = AsyncMock(return_value=send_result)

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
    mock_gateway.send_command = mock_send
    stream_valid = MockGrpcStream(request=pb.DigitalWrite(pin=13, value=1), metadata={"x-device-id": "dev-1"})
    await local_svc.DigitalWrite(stream_valid.as_stream())
    mock_send.assert_awaited_once()
    assert stream_valid.sent_message.status == "ok"

    # 4. SubscribeConsole explicit device resolution
    stream_sub_no_dev = MockGrpcStream(request=pb.SubscribeRequest(), metadata={})
    with pytest.raises(GRPCError) as sub_exc_info:
        await local_svc.SubscribeConsole(stream_sub_no_dev.as_stream())
    assert sub_exc_info.value.status == Status.INVALID_ARGUMENT

    stream_sub_unknown = MockGrpcStream(request=pb.SubscribeRequest(), metadata={"x-device-id": "dev-missing"})
    with pytest.raises(GRPCError) as sub_unavail:
        await local_svc.SubscribeConsole(stream_sub_unknown.as_stream())
    assert sub_unavail.value.status == Status.UNAVAILABLE

    # 5. Metadata resolution edge cases
    stream_empty_meta = MockGrpcStream(metadata=None)
    assert extract_device_id_from_metadata(stream_empty_meta.as_stream().metadata) is None

    stream_empty_list = MockGrpcStream(metadata={"x-device-id": []})
    assert extract_device_id_from_metadata(stream_empty_list.as_stream().metadata) is None

    stream_bytes_meta = MockGrpcStream(metadata={"device-id": b"dev-bytes"})
    assert extract_device_id_from_metadata(stream_bytes_meta.as_stream().metadata) == "dev-bytes"

    stream_str_meta = MockGrpcStream(metadata={"device_id": "dev-str"})
    assert extract_device_id_from_metadata(stream_str_meta.as_stream().metadata) == "dev-str"

    stream_list_bytes = MockGrpcStream(metadata={"x-device-id": [b"dev-first"]})
    assert extract_device_id_from_metadata(stream_list_bytes.as_stream().metadata) == "dev-first"

    # 6. Stream returns None (client disconnect before sending request)
    stream_none = MockGrpcStream(request=None, metadata={"x-device-id": "dev-1"})
    await local_svc.DigitalWrite(stream_none.as_stream())
    stream_none.send_message.assert_not_called()

    # 7. Error forwarding branches (non-200 status code and TimeoutError)
    stream_err = MockGrpcStream(request=pb.DigitalWrite(pin=13, value=1), metadata={"x-device-id": "dev-1"})
    mock_send_err = AsyncMock(return_value=pb.CommandResponse(status_code=500, error_message="fail"))
    mock_gateway.send_command = mock_send_err
    await local_svc.DigitalWrite(stream_err.as_stream())
    assert stream_err.sent_message.status == "error"

    stream_err.reset()
    mock_send_timeout = AsyncMock(side_effect=TimeoutError("timed out"))
    mock_gateway.send_command = mock_send_timeout
    await local_svc.DigitalWrite(stream_err.as_stream())
    assert stream_err.sent_message.status == "error"

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
        rpc_stream = MockGrpcStream(request=req_msg, metadata={"x-device-id": "dev-1"})
        mock_gateway.send_command = AsyncMock(
            return_value=pb.CommandResponse(status_code=200, payload=resp_msg.SerializeToString())
        )
        await rpc_fn(rpc_stream.as_stream())
        assert rpc_stream.send_message.called

    # 9. Publish RPC (console routing and missing device_id)
    stream_pub_no_dev = MockGrpcStream(request=pb.CloudQueuedPublish(topic_name="br/test"), metadata={})
    with pytest.raises(GRPCError) as pub_no_dev_exc:
        await local_svc.Publish(stream_pub_no_dev.as_stream())
    assert pub_no_dev_exc.value.status == Status.INVALID_ARGUMENT

    stream_pub_none = MockGrpcStream(request=None, metadata={"x-device-id": "dev-1"})
    await local_svc.Publish(stream_pub_none.as_stream())

    # Console queue subscription and publishing flow
    console_q: asyncio.Queue[pb.CloudQueuedPublish] = asyncio.Queue()
    mock_gateway.console_queues["dev-1"] = [console_q]
    pub_console_msg = pb.CloudQueuedPublish(topic_name="br/console/write", payload=b"ping")
    stream_pub_console = MockGrpcStream(request=pub_console_msg, metadata={"x-device-id": "dev-1"})
    mock_gateway.send_command = AsyncMock(
        return_value=pb.CommandResponse(status_code=200, payload=pub_console_msg.SerializeToString())
    )
    await local_svc.Publish(stream_pub_console.as_stream())
    assert not console_q.empty()
    queued_item = console_q.get_nowait()
    assert queued_item.topic_name == "br/console/write"

    # 10. SubscribeConsole active delivery and termination
    stream_sub_active = MockGrpcStream(
        request=pb.SubscribeRequest(),
        metadata={"x-device-id": "dev-1"},
        send_exception=RuntimeError("stream disconnect"),
    )

    async def _feed_console() -> None:
        await asyncio.sleep(0.01)
        for q in mock_gateway.console_queues.get("dev-1", []):
            q.put_nowait(pb.CloudQueuedPublish(topic_name="br/console/out", payload=b"hello"))

    asyncio.create_task(_feed_console())
    await local_svc.SubscribeConsole(stream_sub_active.as_stream())
    assert stream_sub_active.send_message.called

    # SubscribeConsole when recv_message returns None
    stream_sub_none = MockGrpcStream(request=None, metadata={"x-device-id": "dev-1"})
    await local_svc.SubscribeConsole(stream_sub_none.as_stream())

    # 11. Publish with non-console topic (covers branch 707->710)
    pub_data_msg = pb.CloudQueuedPublish(topic_name="br/telemetry/data", payload=b"123")
    stream_pub_non_console = MockGrpcStream(request=pub_data_msg, metadata={"x-device-id": "dev-1"})
    mock_gateway.send_command = AsyncMock(
        return_value=pb.CommandResponse(status_code=200, payload=pub_data_msg.SerializeToString())
    )
    await local_svc.Publish(stream_pub_non_console.as_stream())

    # 12. SubscribeConsole when device_id removed from console_queues before exit (covers branch 738)
    async def _fail_and_clear_queues(_msg: Any) -> None:
        mock_gateway.console_queues.pop("dev-1", None)
        raise RuntimeError("stream aborted")

    stream_sub_cleanup = MockGrpcStream(
        request=pb.SubscribeRequest(),
        metadata={"x-device-id": "dev-1"},
        send_side_effect=_fail_and_clear_queues,
    )

    async def _feed_console_cleanup() -> None:
        await asyncio.sleep(0.01)
        for q in mock_gateway.console_queues.get("dev-1", []):
            q.put_nowait(pb.CloudQueuedPublish(topic_name="br/console/out", payload=b"bye"))

    asyncio.create_task(_feed_console_cleanup())
    await local_svc.SubscribeConsole(stream_sub_cleanup.as_stream())


def test_tsdb_sink_invalid_scheme() -> None:
    with pytest.raises(ValueError, match="Invalid TSDB scheme 'file'"):
        TSDBSink(endpoint_url="file:///etc/passwd")
    with pytest.raises(ValueError, match="Invalid TSDB scheme 'ftp'"):
        TSDBSink(endpoint_url="ftp://evil.com/write")


async def test_protobuf_gateway_reflection_services() -> None:
    gw = ProtobufGateway(use_tls=False)
    services = [CloudBridgeService(gw), GatewayLocalBridgeService(gw)]
    extended = ServerReflection.extend(services)
    assert extended == HasLen(4)
    service_names = [type(s).__name__ for s in extended]
    assert service_names == Contains("ServerReflection")


async def test_gateway_session_cancelled() -> None:
    gw = ProtobufGateway(use_tls=False)
    svc = CloudBridgeService(gw)

    mock_stream = MockGrpcStream[pb.CloudEnvelope, pb.CloudEnvelope](
        client_cn="test-dev", aiter_exception=asyncio.CancelledError()
    )

    with pytest.raises(asyncio.CancelledError):
        await svc.Session(mock_stream.as_stream())


async def test_gateway_device_session_lifecycle() -> None:
    from gateway import GatewayDeviceSession

    fleet = FleetMetrics()
    fut: asyncio.Future[Any] = asyncio.get_running_loop().create_future()
    mock_gw = MagicMock(pending_commands={("dev-test", 1): fut})
    session = GatewayDeviceSession(device_id="dev-test", metrics=fleet, gateway=mock_gw)
    assert session.fsm.current_state_value == "connected"
    session.fsm.activate()
    assert session.fsm.current_state_value == "active"

    q: asyncio.Queue[pb.CloudQueuedPublish] = asyncio.Queue()
    session.console_queues.append(q)

    session.close()
    assert session.fsm.current_state_value == "closed"
    assert fut.done()
    assert isinstance(fut.exception(), ConnectionResetError)
    assert len(session.console_queues) == 0
    # Closing an already closed session is a safe no-op
    session.close()
    assert session.fsm.current_state_value == "closed"
