# pyright: reportPrivateUsage=false
"""Comprehensive unit test suite targeting uncovered branches across McuBridge components."""

from __future__ import annotations

import asyncio
import os
from pathlib import Path
import ssl
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

from google.protobuf.message import Message as ProtobufMessage
import pytest

from gateway import CloudBridgeService, ProtobufGateway
from mcubridge.config.settings import RuntimeConfig
from mcubridge.daemon import app as daemon_app, run_daemon
from mcubridge.metrics import (
    PrometheusExporter,
    RuntimeStateCollector,
    _emit_bridge_snapshot,
    publish_bridge_snapshots,
    publish_metrics,
)
from mcubridge.protocol import mcubridge_pb2 as pb
from mcubridge.protocol.protocol import Command, Status
from mcubridge.protocol.topics import get_topic_for_message, parse_topic
from mcubridge.services.handshake import SerialHandshakeManager, derive_serial_timing
from mcubridge.services.runtime import BridgeService, LocalBridgeService, ProcessContext
from mcubridge.state.context import RuntimeState, create_runtime_state
from mcubridge.state.storage import LmdbDeque
from mcubridge.transport.serial import SerialTransport
from mcubridge_client.definitions import build_bridge_args
from mcubridge_client.env import _is_openwrt, dump_client_env, read_uci_general


def _make_config() -> RuntimeConfig:
    return RuntimeConfig(
        allowed_commands=("echo", "ls"),
        serial_shared_secret=b"testsharedsecret",
        allow_non_tmp_paths=True,
        cloud_enabled=True,
        cloud_host="127.0.0.1",
        cloud_port=8443,
        cloud_http3_enabled=True,
        cloud_http3_port=8843,
    )


@pytest.fixture
def test_config() -> RuntimeConfig:
    return _make_config()


@pytest.fixture
def mock_state(test_config: RuntimeConfig) -> RuntimeState:
    return create_runtime_state(test_config)


# ==========================================
# 1. Topics Edge Cases
# ==========================================


def test_topics_get_topic_for_message_int_and_unknown() -> None:
    topic = get_topic_for_message("br", Command.CMD_GET_VERSION_RESP.value)
    assert topic is not None
    assert "version" in topic

    assert get_topic_for_message("br", "non_existent_topic_xyz") is None


def test_topics_parse_topic_mismatched_prefix() -> None:
    assert parse_topic("br", "other_prefix/service/action") is None
    assert parse_topic("br", "") is None
    assert parse_topic("", "br/service/action") is None


# ==========================================
# 2. LocalBridgeService & IPC Edge Paths
# ==========================================


_orig_timeout = asyncio.timeout


@pytest.mark.asyncio
async def test_local_bridge_service_publish_timeout_and_oserror(
    test_config: RuntimeConfig, mock_state: RuntimeState
) -> None:
    serial = AsyncMock(spec=SerialTransport)
    svc = BridgeService(test_config, mock_state, serial)
    local_svc = LocalBridgeService(svc)

    # 1. Simulate timeout waiting on response_queue
    req_msg = pb.CloudQueuedPublish(
        topic_name="br/file/read",
        payload=b"test",
        correlation_data=b"corr-timeout-1",
    )
    mock_stream = AsyncMock()
    mock_stream.recv_message.return_value = req_msg

    def _short_timeout(_t: float) -> Any:
        return _orig_timeout(0.001)

    with patch.object(svc, "handle_request", new_callable=AsyncMock):
        with patch("mcubridge.services.runtime.asyncio.timeout", side_effect=_short_timeout):
            await local_svc.Publish(mock_stream)
            assert mock_stream.send_message.called

    # 2. Simulate OSError during response write
    mock_stream.reset_mock()
    mock_stream.recv_message.return_value = req_msg
    mock_stream.send_message.side_effect = OSError("Socket broken")

    async def _handle_and_reply(req: pb.CloudQueuedPublish) -> None:
        if req.correlation_data in svc.ipc_requests:
            await svc.ipc_requests[req.correlation_data].put(pb.CloudQueuedPublish(topic_name="br/reply"))

    with patch.object(svc, "handle_request", side_effect=_handle_and_reply):
        await local_svc.Publish(mock_stream)
        assert b"corr-timeout-1" not in svc.ipc_requests


@pytest.mark.asyncio
async def test_local_bridge_service_subscribe_console_none_and_exceptions(
    test_config: RuntimeConfig, mock_state: RuntimeState
) -> None:
    serial = AsyncMock(spec=SerialTransport)
    svc = BridgeService(test_config, mock_state, serial)
    local_svc = LocalBridgeService(svc)

    # Recv message returns None
    mock_stream = AsyncMock()
    mock_stream.recv_message.return_value = None
    await local_svc.SubscribeConsole(mock_stream)

    # Recv message followed by RuntimeError in loop
    mock_stream.reset_mock()
    mock_stream.recv_message.return_value = pb.SubscribeRequest()
    mock_stream.send_message.side_effect = RuntimeError("Stream closed")

    async def _feed_queue() -> None:
        for _ in range(50):
            await asyncio.sleep(0.005)
            if svc.console_queues:
                await svc.console_queues[-1].put(pb.CloudQueuedPublish(topic_name="br/console/rx", payload=b"hello"))
                break

    feed_task = asyncio.create_task(_feed_queue())
    with pytest.raises(RuntimeError):
        await local_svc.SubscribeConsole(mock_stream)
    await feed_task


# ==========================================
# 3. Runtime Cloud Spool & Cloud Session
# ==========================================


@pytest.mark.asyncio
async def test_flush_cloud_spool_corrupt_and_index_error(test_config: RuntimeConfig, mock_state: RuntimeState) -> None:
    serial = AsyncMock(spec=SerialTransport)
    svc = BridgeService(test_config, mock_state, serial)
    svc._cloud_stream = AsyncMock()

    mock_spool = MagicMock(spec=LmdbDeque)
    svc._cloud_spool = mock_spool

    # Case 2: spool.peek raises IndexError
    svc.state.cloud_spool_degraded = False
    mock_spool.__len__.return_value = 1
    mock_spool.peek = AsyncMock(side_effect=IndexError("empty"))
    mock_spool.popleft = AsyncMock()
    mock_spool.vacuum = AsyncMock()
    await svc._flush_cloud_spool_locked()
    assert mock_spool.peek.called

    # Case 3: spool.peek returns corrupt data and popleft raises OSError
    mock_spool.__len__.side_effect = [1, 1, 0, 0]
    mock_spool.peek = AsyncMock(return_value=b"not-a-valid-protobuf")
    mock_spool.popleft = AsyncMock(side_effect=OSError("Disk failure"))
    await svc._flush_cloud_spool_locked()
    assert mock_spool.popleft.called

    # Case 4: spool.popleft raises IndexError after publish
    valid_msg = pb.CloudQueuedPublish(topic_name="br/test", payload=b"ok")
    mock_spool.__len__.side_effect = [1, 0, 0, 0]
    mock_spool.peek = AsyncMock(return_value=valid_msg.SerializeToString())
    mock_spool.popleft = AsyncMock(side_effect=IndexError("popped early"))
    with patch.object(svc, "_publish_cloud_message", new_callable=AsyncMock, return_value=True):
        await svc._flush_cloud_spool_locked()
        assert mock_spool.peek.called


class _MockCloudStream:
    def __init__(self, items: list[pb.CloudEnvelope]) -> None:
        self._items = items

    async def __aenter__(self) -> _MockCloudStream:
        return self

    async def __aexit__(self, exc_type: Any, exc_val: Any, exc_tb: Any) -> None:
        pass

    def __aiter__(self) -> _MockCloudStream:
        self._iter = iter(self._items)
        return self

    async def __anext__(self) -> pb.CloudEnvelope:
        try:
            return next(self._iter)
        except StopIteration:
            raise StopAsyncIteration

    async def send_message(self, msg: Any) -> None:
        pass


@pytest.mark.asyncio
async def test_connect_cloud_session_http3(test_config: RuntimeConfig, mock_state: RuntimeState) -> None:
    serial = AsyncMock(spec=SerialTransport)
    svc = BridgeService(test_config, mock_state, serial)

    envelope_pong = pb.CloudEnvelope(protocol_version=2, pong=pb.KeepalivePong())
    envelope_cmd = pb.CloudEnvelope(
        protocol_version=2,
        sequence_id=42,
        command_request=pb.CommandRequest(command_path="system/version/read", payload=b""),
    )

    svc.config.cloud_http3_enabled = True
    with patch("mcubridge.services.runtime.Channel"), patch("mcubridge.services.runtime.CloudBridgeStub") as mock_stub:
        mock_stub.return_value.Session.open.return_value = _MockCloudStream([envelope_pong, envelope_cmd])
        with patch.object(svc, "_send_cloud_event", new_callable=AsyncMock):
            with patch.object(svc, "flush_cloud_spool", new_callable=AsyncMock):
                with patch.object(svc, "_cloud_incoming_worker", new_callable=AsyncMock):
                    await svc.connect_cloud_session(ssl.create_default_context())
                    assert svc.state.connected_via_http3 is True
                    assert not svc._cloud_incoming_queue.empty()


@pytest.mark.asyncio
async def test_connect_cloud_session_http2_fallback(test_config: RuntimeConfig, mock_state: RuntimeState) -> None:
    serial = AsyncMock(spec=SerialTransport)
    svc = BridgeService(test_config, mock_state, serial)

    svc.config.cloud_http3_enabled = False
    with patch("mcubridge.services.runtime.Channel"), patch("mcubridge.services.runtime.CloudBridgeStub") as mock_stub:
        mock_stub.return_value.Session.open.return_value = _MockCloudStream([])
        with patch.object(svc, "_send_cloud_event", new_callable=AsyncMock):
            with patch.object(svc, "flush_cloud_spool", new_callable=AsyncMock):
                with patch.object(svc, "_cloud_incoming_worker", new_callable=AsyncMock):
                    await svc.connect_cloud_session(ssl.create_default_context())
                    assert svc.state.connected_via_http3 is False


@pytest.mark.asyncio
async def test_run_cloud_retryer_fatal_exception(test_config: RuntimeConfig, mock_state: RuntimeState) -> None:
    serial = AsyncMock(spec=SerialTransport)
    svc = BridgeService(test_config, mock_state, serial)

    with patch("mcubridge.services.runtime.get_ssl_context", return_value=None):
        with patch("tenacity.AsyncRetrying.__call__", side_effect=ConnectionError("Fatal cloud error")):
            with pytest.raises(ConnectionError):
                await svc.run_cloud()


# ==========================================
# 4. Metrics & Exporter Edge Paths
# ==========================================


@pytest.mark.asyncio
async def test_emit_bridge_snapshot_attribute_error(mock_state: RuntimeState) -> None:
    enqueue = AsyncMock()
    with patch.object(mock_state, "build_bridge_snapshot", side_effect=AttributeError("Missing attr")):
        await _emit_bridge_snapshot(mock_state, enqueue, flavor="summary")
        assert enqueue.call_count == 0


@pytest.mark.asyncio
async def test_publish_metrics_tick_error(mock_state: RuntimeState) -> None:
    enqueue = AsyncMock()
    with patch("mcubridge.metrics._emit_metrics_snapshot", side_effect=RuntimeError("Tick error")):
        task = asyncio.create_task(publish_metrics(mock_state, enqueue, interval=0.01, min_interval=0.01))
        await asyncio.sleep(0.05)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task


@pytest.mark.asyncio
async def test_publish_bridge_snapshots_both_disabled(mock_state: RuntimeState) -> None:
    enqueue = AsyncMock()
    task = asyncio.create_task(
        publish_bridge_snapshots(mock_state, enqueue, summary_interval=0.0, handshake_interval=0.0)
    )
    await asyncio.sleep(0.02)
    assert not task.done()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task


def test_runtime_state_collector_dead_ref() -> None:
    collector = RuntimeStateCollector(MagicMock())
    object.__setattr__(collector, "_state_ref", lambda: None)
    assert list(collector.collect()) == []


def test_prometheus_exporter_port_unbound(mock_state: RuntimeState) -> None:
    with patch("mcubridge.metrics.make_server", return_value=None):
        exporter = PrometheusExporter(mock_state, host="127.0.0.1", port=9999)
        assert exporter.port == 9999


@pytest.mark.asyncio
async def test_prometheus_exporter_unregister_keyerror(mock_state: RuntimeState) -> None:
    mock_srv = MagicMock()
    with patch("mcubridge.metrics.make_server", return_value=mock_srv):
        exporter = PrometheusExporter(mock_state, host="127.0.0.1", port=0)
        exporter._registry = MagicMock()
        exporter._registry.unregister.side_effect = KeyError("Not registered")

        with patch("asyncio.to_thread", side_effect=asyncio.CancelledError()):
            with pytest.raises(asyncio.CancelledError):
                await exporter.run()
        assert mock_srv.server_close.called


# ==========================================
# 5. Serial Transport & Handshake Edge Paths
# ==========================================


@pytest.mark.asyncio
async def test_serial_transport_toggle_dtr_error(test_config: RuntimeConfig, mock_state: RuntimeState) -> None:
    transport = SerialTransport(test_config, mock_state, None)
    mock_serial = AsyncMock()
    mock_serial.set_modem_pins.side_effect = OSError("I/O error")
    transport.serial = mock_serial
    await transport._toggle_dtr()
    assert mock_serial.set_modem_pins.called


def test_serial_transport_switch_local_baudrate_error(test_config: RuntimeConfig, mock_state: RuntimeState) -> None:
    transport = SerialTransport(test_config, mock_state, None)
    transport.serial = MagicMock()
    type(transport.serial.transport.serial).baudrate = property(
        fget=lambda self: 115200,
        fset=MagicMock(side_effect=ValueError("Invalid baud")),
    )
    with pytest.raises(RuntimeError):
        transport._switch_local_baudrate(99999999)


@pytest.mark.asyncio
async def test_serial_transport_send_failure_status_code(test_config: RuntimeConfig, mock_state: RuntimeState) -> None:
    transport = SerialTransport(test_config, mock_state, None)
    mock_serial = MagicMock()
    mock_serial.is_open = True
    transport.serial = mock_serial

    with patch.object(transport, "send_raw", new_callable=AsyncMock, return_value=True):
        send_task = asyncio.create_task(
            transport.send(Command.CMD_SET_PIN_MODE.value, pb.PinMode(pin=13, mode=pb.PIN_OUTPUT))
        )
        await asyncio.sleep(0.01)
        # Correlate failure
        transport._correlate_frame(Status.ERROR.value, b"")
        res = await send_task
        assert res is False


@pytest.mark.asyncio
async def test_handshake_publish_event_empty_topic(test_config: RuntimeConfig, mock_state: RuntimeState) -> None:
    mock_send = AsyncMock(return_value=True)
    mock_ack = AsyncMock(return_value=True)
    mock_enqueue = AsyncMock()

    timing = derive_serial_timing(test_config)
    hm = SerialHandshakeManager(
        config=test_config,
        state=mock_state,
        serial_timing=timing,
        send_frame=mock_send,
        acknowledge_frame=mock_ack,
        enqueue_cloud=mock_enqueue,
    )

    with patch("mcubridge.services.handshake.get_topic_for_message", return_value=None):
        await hm._publish_handshake_event("sync_failed")
        assert not mock_enqueue.called


def test_handshake_calculate_tag_empty_secret() -> None:
    assert SerialHandshakeManager.calculate_handshake_tag(None, b"12345678") == b""
    assert SerialHandshakeManager.calculate_handshake_tag(b"", b"12345678") == b""


# ==========================================
# 6. LMDB Storage Edge Cases
# ==========================================


@pytest.mark.asyncio
async def test_lmdb_deque_peek_pop_none_value(tmp_path: Path) -> None:
    db_path = str(tmp_path / "test_deque")
    deque = LmdbDeque(db_path, maxlen=10)

    # Empty deque raises IndexError on peek and popleft
    with pytest.raises(IndexError):
        await deque.peek()

    with pytest.raises(IndexError):
        await deque.popleft()


# ==========================================
# 7. Daemon Entrypoint & Exception Handling
# ==========================================


def test_daemon_app_cli_invocation_help() -> None:
    with pytest.raises(SystemExit) as exc_info:
        daemon_app(["--help"])
    assert exc_info.value.code == 0


def test_daemon_unhandled_exception_group() -> None:
    with patch("mcubridge.daemon.load_runtime_config") as mock_cfg:
        mock_cfg.return_value = _make_config()
        with patch("mcubridge.daemon.verify_crypto_integrity", return_value=True):
            with patch("mcubridge.daemon.BridgeService") as mock_svc_cls:
                mock_svc = MagicMock()
                mock_svc.run.side_effect = ExceptionGroup("fatal", [ZeroDivisionError("Unhandled")])
                mock_svc_cls.return_value = mock_svc
                with pytest.raises(ExceptionGroup):
                    run_daemon()


# ==========================================
# 8. Client SDK & Env Edge Cases
# ==========================================


def test_client_env_find_spec_none() -> None:
    with patch("importlib.util.find_spec", return_value=None):
        with patch.dict(os.environ, {"MCUBRIDGE_FORCE_UCI": "1"}):
            assert read_uci_general() == {}


def test_client_env_not_callable() -> None:
    mock_mod = MagicMock()
    mock_mod.get_uci_config = "not_callable"
    with patch("importlib.util.find_spec", return_value=MagicMock()):
        with patch("importlib.import_module", return_value=mock_mod):
            with patch.dict(os.environ, {"MCUBRIDGE_FORCE_UCI": "1"}):
                assert read_uci_general() == {}


def test_client_definitions_empty_args() -> None:
    with patch.dict(os.environ, {"MCUBRIDGE_SOCKET_PATH": ""}):
        args = build_bridge_args(socket_path="", topic_prefix="")
        assert "socket_path" in args  # Falls back to default socket path
        assert "topic_prefix" not in args


def test_client_env_is_openwrt_helper() -> None:
    with patch.dict(os.environ, {"MCUBRIDGE_FORCE_UCI": "1"}):
        assert _is_openwrt() is True
    dump_client_env(logger=MagicMock())


# ==========================================
# 9. Gateway Session Cancelled
# ==========================================


@pytest.mark.asyncio
async def test_gateway_session_cancelled() -> None:
    gw = ProtobufGateway(use_tls=False)
    service = CloudBridgeService(gw)

    mock_stream = AsyncMock()
    mock_stream.peer.addr.return_value = ("127.0.0.1", 5000)
    mock_stream.peer.cert.return_value = None

    with patch.object(service, "Session", side_effect=asyncio.CancelledError):
        with pytest.raises(asyncio.CancelledError):
            await service.Session(mock_stream)


# ==========================================
# 10. Runtime Process, Quota, & Dispatch Edges
# ==========================================


@pytest.mark.asyncio
async def test_runtime_write_with_quota(test_config: RuntimeConfig, mock_state: RuntimeState, tmp_path: Path) -> None:
    serial = AsyncMock(spec=SerialTransport)
    svc = BridgeService(test_config, mock_state, serial)

    target_file = tmp_path / "quota_test.bin"

    # Case 1: Disk full (free < len(data))
    with patch("shutil.disk_usage") as mock_usage:
        mock_usage.return_value = MagicMock(free=5, used=100, total=105)
        res = await svc._write_with_quota(target_file, b"1234567890")
        assert res is False
        assert svc.state.file_storage_limit_rejections == 1

    # Case 2: Disk usage check raises OSError
    with patch("shutil.disk_usage", side_effect=OSError("Stat failure")):
        res = await svc._write_with_quota(target_file, b"data")
        assert res is True
        assert target_file.read_bytes() == b"data"


@pytest.mark.asyncio
async def test_runtime_run_process_oserror_and_not_allowed(
    test_config: RuntimeConfig, mock_state: RuntimeState
) -> None:
    serial = AsyncMock(spec=SerialTransport)
    svc = BridgeService(test_config, mock_state, serial)

    # Command not allowed
    pid = await svc._run_process("forbidden_cmd_xyz")
    assert pid == 0

    # Subprocess creation raises OSError
    with patch("asyncio.create_subprocess_exec", side_effect=OSError("Exec failed")):
        pid = await svc._run_process("echo hello")
        assert pid == 0


@pytest.mark.asyncio
async def test_runtime_terminate_process_escalation(test_config: RuntimeConfig, mock_state: RuntimeState) -> None:
    serial = AsyncMock(spec=SerialTransport)
    svc = BridgeService(test_config, mock_state, serial)

    mock_handle = AsyncMock()
    mock_handle.returncode = None
    mock_handle.pid = 12345
    mock_handle.wait.side_effect = [TimeoutError(), TimeoutError()]

    ctx = ProcessContext(mock_handle)

    with patch("os.killpg") as mock_killpg:
        code = await svc._terminate_process(12345, ctx, grace_period=0.01)
        assert code == -1
        assert mock_killpg.call_count >= 2


@pytest.mark.asyncio
async def test_runtime_flush_console_queue_send_failed(test_config: RuntimeConfig, mock_state: RuntimeState) -> None:
    serial = AsyncMock(spec=SerialTransport)
    serial.send.return_value = False
    svc = BridgeService(test_config, mock_state, serial)

    svc.state.console_to_mcu_queue.append(b"console payload")
    await svc._flush_console_queue()
    assert len(svc.state.console_to_mcu_queue) == 1


@pytest.mark.asyncio
async def test_runtime_reject_cloud_topic_variants(test_config: RuntimeConfig, mock_state: RuntimeState) -> None:
    serial = AsyncMock(spec=SerialTransport)
    svc = BridgeService(test_config, mock_state, serial)
    with patch.object(svc, "enqueue_cloud", new_callable=AsyncMock) as mock_enqueue:
        from mcubridge.protocol.protocol import Topic

        await svc._reject_cloud(pb.CloudQueuedPublish(), Topic.DIGITAL, "write")
        assert mock_enqueue.called

        mock_enqueue.reset_mock()
        await svc._reject_cloud(pb.CloudQueuedPublish(), "custom_topic", "read")
        assert mock_enqueue.called


# ==========================================
# 11. Runtime Cloud Spool Trim & Envelope Correlation
# ==========================================


@pytest.mark.asyncio
async def test_runtime_cloud_spool_trimming_and_drop(test_config: RuntimeConfig, mock_state: RuntimeState) -> None:
    serial = AsyncMock(spec=SerialTransport)
    svc = BridgeService(test_config, mock_state, serial)

    svc.state.cloud_queue_limit = 2
    mock_spool = MagicMock(spec=LmdbDeque)
    svc._cloud_spool = mock_spool

    # Simulate spool length decreasing below limit to exit while loop
    mock_spool.__len__.side_effect = [3, 1, 1]
    mock_spool.popleft = AsyncMock(return_value=b"old")
    mock_spool.append = AsyncMock(return_value=None)

    msg = pb.CloudQueuedPublish(topic_name="br/test", payload=b"data")
    res = await svc._spool_cloud_message_locked(msg)
    assert res is True
    assert svc.state.cloud_spool_dropped_limit == 1
    assert svc.state.cloud_spool_trim_events == 1


@pytest.mark.asyncio
async def test_runtime_publish_cloud_message_with_correlation(
    test_config: RuntimeConfig, mock_state: RuntimeState
) -> None:
    serial = AsyncMock(spec=SerialTransport)
    svc = BridgeService(test_config, mock_state, serial)
    mock_stream = AsyncMock()
    svc._cloud_stream = mock_stream

    # Message with correlation_data (simulating RPC command response)
    msg = pb.CloudQueuedPublish(
        topic_name="br/test/response",
        payload=b"response_payload",
        correlation_data=(12345).to_bytes(8, "big"),
    )
    res = await svc._publish_cloud_message(msg)
    assert res is True
    assert mock_stream.send_message.called


# ==========================================
# 12. Runtime IPC Server Socket Cleanup & Chmod Error
# ==========================================


@pytest.mark.asyncio
async def test_runtime_ipc_server_lifecycle(
    test_config: RuntimeConfig, mock_state: RuntimeState, tmp_path: Path
) -> None:
    serial = AsyncMock(spec=SerialTransport)
    svc = BridgeService(test_config, mock_state, serial)

    mock_srv = MagicMock()
    mock_srv.start = AsyncMock()
    mock_srv.wait_closed = AsyncMock()
    mock_srv.close = MagicMock()

    sock_file = tmp_path / "test_ipc.sock"
    with patch.dict(os.environ, {"MCUBRIDGE_SOCKET_PATH": str(sock_file)}):
        with patch("mcubridge.services.runtime.Server", return_value=mock_srv):
            with patch("pathlib.Path.unlink", side_effect=[OSError("Cannot unlink"), None]):
                with patch("os.chmod", side_effect=OSError("Chmod error")):
                    await svc.run_ipc_server()
                    assert mock_srv.start.called


# ==========================================
# 13. Runtime MCU Handlers Error & Edge Paths
# ==========================================


@pytest.mark.asyncio
async def test_runtime_handle_process_kill_paths(test_config: RuntimeConfig, mock_state: RuntimeState) -> None:
    serial = AsyncMock(spec=SerialTransport)
    svc = BridgeService(test_config, mock_state, serial)

    kill_req = pb.CloudQueuedPublish(
        topic_name="br/process/kill",
        payload=b"99999",
        correlation_data=b"corr-kill-1",
    )

    # Case 1: Process not found in running_processes
    await svc._handle_shell_kill(99999, kill_req)
    assert 99999 not in svc.state.running_processes

    # Case 2: Process found and terminated with error
    mock_handle = AsyncMock()
    mock_handle.pid = 88888
    mock_handle.returncode = None
    from mcubridge.services.runtime import ProcessContext

    svc.state.running_processes[88888] = ProcessContext(mock_handle)

    with patch.object(svc, "_terminate_process", side_effect=ProcessLookupError("Term failed")):
        await svc._handle_shell_kill(88888, kill_req)
        assert 88888 not in svc.state.running_processes

    # Case 3: _on_mcu_process_kill
    svc.state.running_processes[88888] = ProcessContext(mock_handle)
    await svc._on_mcu_process_kill(10, pb.ProcessKill(pid=88888))
    assert 88888 not in svc.state.running_processes


@pytest.mark.asyncio
async def test_runtime_on_mcu_file_and_datastore_handlers(
    test_config: RuntimeConfig, mock_state: RuntimeState, tmp_path: Path
) -> None:
    test_config.file_system_root = str(tmp_path)
    serial = AsyncMock(spec=SerialTransport)
    serial.send.return_value = True
    svc = BridgeService(test_config, mock_state, serial)
    svc.state.file_system_root = str(tmp_path)

    # File Read
    test_file = tmp_path / "read_target.txt"
    test_file.write_bytes(b"hello world")
    read_req = pb.FileRead(path="read_target.txt")
    await svc._on_mcu_file_read(1, read_req)
    assert serial.send.called

    # File Write with quota failure
    write_req = pb.FileWrite(path="write_target.txt", data=b"data")
    with patch.object(svc, "_write_with_quota", new_callable=AsyncMock, return_value=False):
        await svc._on_mcu_file_write(2, write_req)

    # File Remove
    await svc._on_mcu_file_remove(3, pb.FileRemove(path="read_target.txt"))
    assert not test_file.exists()

    # Datastore Get / Put
    await svc._on_mcu_datastore_put(4, pb.DatastorePut(key="mykey", value=b"myval"))
    assert mock_state.datastore_cache is not None
    assert await mock_state.datastore_cache.get("mykey") == b"myval"
    await svc._on_mcu_datastore_get(5, pb.DatastoreGet(key="mykey"))


# ==========================================
# 14. Serial Transport Limit Overrun & Disconnect Errors
# ==========================================


@pytest.mark.asyncio
async def test_serial_transport_read_loop_limit_overrun(test_config: RuntimeConfig, mock_state: RuntimeState) -> None:
    transport = SerialTransport(test_config, mock_state, None)
    mock_serial = AsyncMock()
    mock_serial.readuntil.side_effect = [
        asyncio.LimitOverrunError("Exceeded limit", 1024),
        asyncio.IncompleteReadError(b"", None),
    ]
    mock_serial.read.return_value = b""

    await transport._read_loop(mock_serial)
    assert transport.state.serial_decode_errors == 1


@pytest.mark.asyncio
async def test_serial_transport_disconnect_handler_exception(
    test_config: RuntimeConfig, mock_state: RuntimeState
) -> None:
    mock_service = AsyncMock()
    mock_service.on_serial_connected.return_value = None
    mock_service.on_serial_disconnected.side_effect = OSError("Teardown error")

    transport = SerialTransport(test_config, mock_state, mock_service)
    transport._stop_event.set()

    mock_async_serial = AsyncMock()
    mock_async_serial.transport = MagicMock()

    class _MockAsyncSerialContext:
        async def __aenter__(self) -> Any:
            return mock_async_serial

        async def __aexit__(self, exc_type: Any, exc_val: Any, exc_tb: Any) -> None:
            pass

    with patch("serialx.AsyncSerial", return_value=_MockAsyncSerialContext()):
        with patch.object(transport, "_toggle_dtr", new_callable=AsyncMock):
            with patch.object(transport, "_read_loop", new_callable=AsyncMock):
                with pytest.raises(ConnectionError):
                    await transport._connect_and_run()


# ==========================================
# 15. Handshake Fault Transitions & Completion
# ==========================================


@pytest.mark.asyncio
async def test_handshake_fault_and_sync_transitions(test_config: RuntimeConfig, mock_state: RuntimeState) -> None:
    mock_send = AsyncMock(return_value=True)
    mock_ack = AsyncMock(return_value=True)
    mock_enqueue = AsyncMock()

    timing = derive_serial_timing(test_config)
    hm = SerialHandshakeManager(
        config=test_config,
        state=mock_state,
        serial_timing=timing,
        send_frame=mock_send,
        acknowledge_frame=mock_ack,
        enqueue_cloud=mock_enqueue,
    )

    from mcubridge.services.handshake import HandshakeState

    # Case 1: send_frame returns False on LINK_RESET
    mock_send.return_value = False
    res = await hm._synchronize_attempt()
    assert res is False

    # Case 2: send_frame returns False on LINK_SYNC
    mock_send.side_effect = [True, False]
    res = await hm._synchronize_attempt()
    assert res is False

    # Case 3: Race condition to FAULT state
    mock_send.side_effect = None
    mock_send.return_value = True
    hm.fsm_state = HandshakeState.FAULT
    with patch.object(hm, "_wait_for_link_sync_confirmation", return_value=False):
        res = await hm._synchronize_attempt()
        assert res is False


# ==========================================
# 16. LmdbCache Error Recovery & Key-Value Operations
# ==========================================


@pytest.mark.asyncio
async def test_lmdb_cache_error_recovery_and_operations(tmp_path: Path) -> None:
    from mcubridge.state.storage import LmdbCache

    cache_path = str(tmp_path / "cache_test")

    # In-memory cache operations
    mem_cache = LmdbCache(":memory:")
    await mem_cache.set("k1", b"v1")
    assert await mem_cache.get("k1") == b"v1"
    assert await mem_cache.get("missing", b"default") == b"default"
    await mem_cache.clear()
    assert await mem_cache.get("k1") is None

    # Disk cache operations
    disk_cache = LmdbCache(cache_path)
    await disk_cache.set("dk1", b"dv1")
    assert await disk_cache.get("dk1") == b"dv1"

    # Simulate get error in disk cache with mock env
    mock_env = MagicMock()
    mock_env.begin.side_effect = OSError("Read error")
    disk_cache.env = mock_env
    val = await disk_cache.get("dk1", b"fallback")
    assert val == b"fallback"

    disk_cache.env = None
    # Verify set and get when env is closed
    await disk_cache.set("k", b"v")
    assert await disk_cache.get("k", b"none") == b"none"


# ==========================================
# 17. Context Reconfigure & Cleanup Edge Paths
# ==========================================


def test_context_configure_and_cleanup_exceptions(test_config: RuntimeConfig, mock_state: RuntimeState) -> None:
    # 1. Simulate exception in _safe_close during configure
    mock_resource = MagicMock()
    mock_resource.close.side_effect = RuntimeError("Close error")
    mock_state.mailbox_queue = mock_resource
    mock_state.configure()

    # 2. Cleanup with process termination exception
    mock_ctx = MagicMock()
    mock_ctx.handle.terminate.side_effect = ProcessLookupError("No such process")
    mock_state.running_processes[9999] = mock_ctx

    mock_state.cloud_publish_queue.put_nowait(pb.CloudQueuedPublish())
    mock_state.cleanup()
    assert len(mock_state.running_processes) == 0


# ==========================================
# 18. Status Writer Cancellation
# ==========================================


@pytest.mark.asyncio
async def test_status_writer_cancellation(mock_state: RuntimeState) -> None:
    from mcubridge.state.status import status_writer

    task = asyncio.create_task(status_writer(mock_state, interval=1))
    await asyncio.sleep(0.02)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task


# ==========================================
# 19. Structures & Context Resolution
# ==========================================


def test_structures_ssl_context_insecure_and_certfile(tmp_path: Path) -> None:
    from mcubridge.protocol.structures import get_ssl_context

    cfg = pb.RuntimeConfig(cloud_tls=True, cloud_tls_insecure=True)
    ctx = get_ssl_context(cfg)
    assert ctx is not None
    assert ctx.check_hostname is False
    assert ctx.verify_mode == ssl.CERT_NONE

    # Non-existent CA file raises RuntimeError
    cfg_invalid = pb.RuntimeConfig(cloud_tls=True, cloud_cafile="/non_existent_ca.crt")
    with pytest.raises(RuntimeError):
        get_ssl_context(cfg_invalid)


def test_structures_resolve_cloud_context_with_properties() -> None:
    from mcubridge.protocol.structures import replace_cloud_publish, resolve_cloud_context

    base_msg = pb.CloudQueuedPublish(topic_name="br/test", payload=b"hello")

    # replace_cloud_publish with user_properties and subscription_identifier
    updated = replace_cloud_publish(
        base_msg,
        user_properties=[("key1", "val1")],
        subscription_identifier=[1, 2, 3],
    )
    assert len(updated.user_properties) == 1
    assert list(updated.subscription_identifier) == [1, 2, 3]

    # resolve_cloud_context with complex context
    class MockContext:
        def __init__(self) -> None:
            self.topic = "custom/request/topic"
            self.properties = MagicMock(ResponseTopic="reply/topic", CorrelationData=b"corr123")

    resolved = resolve_cloud_context(base_msg, MockContext())
    assert resolved.topic_name == "reply/topic"
    assert resolved.correlation_data == b"corr123"
    assert any(p.key == "bridge-request-topic" for p in resolved.user_properties)


# ==========================================
# 20. Config & Settings Factory Edge Cases
# ==========================================


def test_settings_factory_and_json_loading() -> None:
    from mcubridge.config.settings import (
        _runtime_config_factory,
        load_runtime_config,
        load_runtime_config_from_json,
    )

    # Factory with pre-built message
    existing_msg = pb.RuntimeConfig(serial_port="/dev/ttyS0")
    assert _runtime_config_factory(pb_msg=existing_msg) is existing_msg

    # Loading from JSON with overrides
    json_data = '{"serial_port": "/dev/ttyACM0", "cloud_enabled": false}'
    cfg_from_json = load_runtime_config_from_json(json_data, overrides={"cloud_enabled": True})
    assert cfg_from_json.serial_port == "/dev/ttyACM0"
    assert cfg_from_json.cloud_enabled is True

    # Loading from Dict
    cfg_from_dict = load_runtime_config_from_json({"serial_port": "/dev/ttyUSB0"})
    assert cfg_from_dict.serial_port == "/dev/ttyUSB0"

    # UCI invalid config fatal handling
    with patch("mcubridge.config.settings._load_raw_config", return_value=({"serial_port": "/dev/ttyATH0"}, "uci")):
        with patch("mcubridge.config.settings.validate_config", side_effect=ValueError("Invalid UCI field")):
            with pytest.raises(RuntimeError) as exc_info:
                load_runtime_config()
            assert "Invalid system configuration" in str(exc_info.value)


# ==========================================
# 21. Runtime Process, SPI, Pin & File Coverage Boost
# ==========================================


@pytest.mark.asyncio
async def test_runtime_monitor_process_timeout_escalation(test_config: RuntimeConfig, mock_state: RuntimeState) -> None:
    serial = AsyncMock(spec=SerialTransport)
    svc = BridgeService(test_config, mock_state, serial)

    mock_handle = AsyncMock()
    mock_handle.pid = 4444
    mock_handle.wait.side_effect = TimeoutError("Wait timeout")

    from mcubridge.services.runtime import ProcessContext

    ctx = ProcessContext(mock_handle)
    svc.state.running_processes[4444] = ctx

    with patch.object(svc, "_terminate_process", new_callable=AsyncMock, return_value=-9):
        with patch("asyncio.sleep", new_callable=AsyncMock):
            await svc._monitor_process(4444)
            assert ctx.exit_code == -9


@pytest.mark.asyncio
async def test_runtime_poll_process_stream_timeout_and_eof(
    test_config: RuntimeConfig, mock_state: RuntimeState
) -> None:
    serial = AsyncMock(spec=SerialTransport)
    svc = BridgeService(test_config, mock_state, serial)

    # 1. Process not found
    res = await svc._poll_process(99999)
    assert res.status == Status.ERROR.value
    assert res.finished is True

    # 2. Stream read timeout and finished EOF
    mock_handle = MagicMock()
    mock_handle.pid = 5555
    mock_handle.returncode = 0

    mock_stdout = MagicMock()
    mock_stdout.at_eof = MagicMock(return_value=False)
    mock_stdout.read = AsyncMock(side_effect=TimeoutError("Stream timeout"))

    mock_stderr = MagicMock()
    mock_stderr.at_eof = MagicMock(return_value=True)
    mock_stderr.read = AsyncMock(return_value=b"")

    mock_handle.stdout = mock_stdout
    mock_handle.stderr = mock_stderr

    from mcubridge.services.runtime import ProcessContext

    ctx = ProcessContext(mock_handle)
    ctx.exit_code = 0
    svc.state.running_processes[5555] = ctx

    res2 = await svc._poll_process(5555)
    assert res2.status == Status.OK.value


@pytest.mark.asyncio
async def test_runtime_handle_request_link_sync_timeout_and_reject(
    test_config: RuntimeConfig, mock_state: RuntimeState
) -> None:
    serial = AsyncMock(spec=SerialTransport)
    svc = BridgeService(test_config, mock_state, serial)
    svc.state.cloud_topic_prefix = "br"

    # Disallow digital_write
    assert svc.state.topic_authorization is not None
    svc.state.topic_authorization.digital_write = False
    svc.state.link_sync_event.clear()
    req = pb.CloudQueuedPublish(topic_name="br/d/13/write", payload=b"1")

    def _fast_timeout(_t: float) -> Any:
        return _orig_timeout(0.001)

    with patch("mcubridge.services.runtime.asyncio.timeout", side_effect=_fast_timeout):
        with patch.object(svc, "_reject_cloud", new_callable=AsyncMock) as mock_reject:
            await svc.handle_request(req)
            assert mock_reject.called


@pytest.mark.asyncio
async def test_runtime_handle_spi_and_pin_edge_cases(test_config: RuntimeConfig, mock_state: RuntimeState) -> None:
    from mcubridge.protocol.protocol import Topic

    serial = AsyncMock(spec=SerialTransport)
    serial.send.return_value = True
    svc = BridgeService(test_config, mock_state, serial)

    from mcubridge.protocol.topics import TopicRoute, parse_topic

    # SPI Begin, End, Config (invalid payload)
    route_begin = parse_topic("br", "br/spi/begin")
    assert route_begin is not None
    await svc._handle_spi(route_begin, pb.CloudQueuedPublish())

    route_end = parse_topic("br", "br/spi/end")
    assert route_end is not None
    await svc._handle_spi(route_end, pb.CloudQueuedPublish())

    route_cfg = parse_topic("br", "br/spi/config")
    assert route_cfg is not None
    await svc._handle_spi(route_cfg, pb.CloudQueuedPublish(payload=b"invalid-proto"))

    # Pin handling with invalid pin number (<0)
    route_invalid_pin = TopicRoute(raw="br/digital/-1/mode", prefix="br", topic=Topic.DIGITAL, segments=("-1", "mode"))
    await svc._handle_pin(route_invalid_pin, pb.CloudQueuedPublish(payload=b"1"))

    # Serial is None branches
    svc.serial = None
    await svc._handle_spi(route_begin, pb.CloudQueuedPublish())
    await svc._handle_pin(route_begin, pb.CloudQueuedPublish())
    await svc._flush_console_queue()
    assert await svc._request_mcu_version() is False


# ==========================================
# 22. Handshake, Metrics & Context Edge Coverage
# ==========================================


@pytest.mark.asyncio
async def test_handshake_synchronize_fault_race_and_rate_limit(
    test_config: RuntimeConfig, mock_state: RuntimeState
) -> None:
    import time
    from mcubridge.services.handshake import HandshakeState, SerialHandshakeManager

    async def _mock_send_frame(command_id: int, payload: bytes | ProtobufMessage, seq_id: int | None = None) -> bool:
        if command_id == Command.CMD_LINK_SYNC.value:
            fsm.fsm_state = HandshakeState.FAULT
        return True

    fsm = SerialHandshakeManager(
        config=test_config,
        state=mock_state,
        serial_timing=pb.HandshakeConfig(),
        send_frame=_mock_send_frame,
        enqueue_cloud=AsyncMock(),
        acknowledge_frame=AsyncMock(),
    )

    # 1. Simulate fault state right after sending sync
    assert await fsm._synchronize_attempt() is False

    # 2. handle_link_sync_resp rate limit branch
    fsm._state.link_handshake_nonce = b"1234567812345678"
    fsm._config.serial_handshake_min_interval = 100.0
    fsm._state.handshake_rate_until = time.monotonic() + 50.0

    resp_payload = pb.LinkSync(nonce=b"1234567812345678", tag=b"1234567812345678").SerializeToString()
    assert await fsm.handle_link_sync_resp(1, resp_payload) is False

    # 3. handle_capabilities_resp with active future
    loop = asyncio.get_running_loop()
    fsm._capabilities_future = loop.create_future()
    cap_msg = pb.Capabilities(ver=2, arch=1, dig=14, ana=6)
    assert await fsm.handle_capabilities_resp(1, cap_msg) is True
    assert fsm._capabilities_future.done()
    assert fsm._capabilities_future.result() == cap_msg


def test_logging_var_run_log_fallback(test_config: RuntimeConfig) -> None:
    from mcubridge.config.logging import configure_logging

    def _mock_exists(path: Path) -> bool:
        return str(path) == "/var/run/log"

    with patch.object(Path, "exists", autospec=True, side_effect=_mock_exists):
        with patch("mcubridge.config.logging.SysLogHandler", autospec=True) as mock_syslog:
            with patch.dict(os.environ, {}, clear=True):
                configure_logging(test_config)
                assert mock_syslog.called


def test_context_spool_mkdir_oserror_fallback_and_snapshots(test_config: RuntimeConfig) -> None:
    from mcubridge.state.context import RuntimeState

    state = RuntimeState(
        file_system_root="/tmp/mcubridge_test_fs",
        allow_non_tmp_paths=True,
    )
    with patch.object(Path, "mkdir", side_effect=OSError("Permission denied")):
        state.configure()
        assert state.mailbox_queue is not None
        assert state.datastore_cache is None

    # Pipeline snapshot with empty/None event
    snapshot = state.build_serial_pipeline_snapshot()
    assert snapshot is not None


# ==========================================
# 23. Runtime Cloud Spool Errors & Message Publishing
# ==========================================


@pytest.mark.asyncio
async def test_runtime_cloud_spool_locked_lmdb_errors(test_config: RuntimeConfig, mock_state: RuntimeState) -> None:
    import lmdb

    serial = AsyncMock(spec=SerialTransport)
    svc = BridgeService(test_config, mock_state, serial)
    mock_stream = AsyncMock()
    svc._cloud_stream = mock_stream

    mock_spool = MagicMock()
    mock_spool.__len__.return_value = 1
    mock_spool.peek = AsyncMock(side_effect=lmdb.Error("Disk I/O error"))
    svc._cloud_spool = mock_spool

    await svc._flush_cloud_spool_locked()
    assert mock_state.cloud_spool_degraded is True


@pytest.mark.asyncio
async def test_runtime_publish_cloud_message_flavors(test_config: RuntimeConfig, mock_state: RuntimeState) -> None:
    serial = AsyncMock(spec=SerialTransport)
    svc = BridgeService(test_config, mock_state, serial)
    mock_stream = AsyncMock()
    svc._cloud_stream = mock_stream

    # 1. Telemetry report with "metrics" topic
    msg_metrics = pb.CloudQueuedPublish(topic_name="bridge/metrics", payload=b"metrics_data")
    assert await svc._publish_cloud_message(msg_metrics) is True

    # 2. Telemetry report with "summary" topic
    msg_summary = pb.CloudQueuedPublish(topic_name="bridge/summary", payload=b"summary_data")
    assert await svc._publish_cloud_message(msg_summary) is True

    # 3. Telemetry report with "handshake" topic
    msg_handshake = pb.CloudQueuedPublish(topic_name="bridge/handshake", payload=b"handshake_data")
    assert await svc._publish_cloud_message(msg_handshake) is True

    # 4. Stream send raising OSError
    mock_stream.send_message.side_effect = OSError("Socket write failed")
    assert await svc._publish_cloud_message(msg_summary) is False


@pytest.mark.asyncio
async def test_runtime_cloud_incoming_worker_error_logged(test_config: RuntimeConfig, mock_state: RuntimeState) -> None:
    serial = AsyncMock(spec=SerialTransport)
    svc = BridgeService(test_config, mock_state, serial)

    req = pb.CloudQueuedPublish(topic_name="bridge/invalid", payload=b"payload")
    svc._cloud_incoming_queue.put_nowait(req)

    with patch.object(svc, "handle_request", side_effect=ValueError("Test value error")):
        worker_task = asyncio.create_task(svc._cloud_incoming_worker())
        await asyncio.sleep(0.05)
        worker_task.cancel()
        await worker_task
        assert svc._cloud_incoming_queue.empty()


@pytest.mark.asyncio
async def test_serial_transport_connect_and_run_edge_paths(
    test_config: RuntimeConfig, mock_state: RuntimeState
) -> None:
    cfg = pb.RuntimeConfig()
    cfg.CopyFrom(test_config)
    cfg.serial_baud = 230400
    cfg.serial_safe_baud = 115200

    transport = SerialTransport(cfg, mock_state, None)

    # 1. Baud negotiation fails
    with patch("serialx.AsyncSerial") as mock_serialx:
        mock_instance = AsyncMock()
        mock_serialx.return_value.__aenter__.return_value = mock_instance
        with patch.object(transport, "_toggle_dtr", new_callable=AsyncMock):
            with patch.object(transport, "_read_loop", new_callable=AsyncMock):
                with patch.object(transport, "_negotiate_baudrate", new_callable=AsyncMock, return_value=False):
                    transport._stop_event.set()
                    with pytest.raises(ConnectionError) as exc_info:
                        await transport._connect_and_run()
                    assert "Baudrate negotiation failed" in str(exc_info.value)


# ==========================================
# 24. Metrics Snapshots, Status & Daemon Edge Boost
# ==========================================


@pytest.mark.asyncio
async def test_metrics_publish_bridge_snapshots_error_recovery_and_shutdown(
    mock_state: RuntimeState,
) -> None:
    from mcubridge.metrics import publish_bridge_snapshots

    enqueue = AsyncMock()

    with patch("mcubridge.metrics._emit_bridge_snapshot", side_effect=RuntimeError("Snapshot failure")):
        task = asyncio.create_task(
            publish_bridge_snapshots(mock_state, enqueue, summary_interval=0.01, handshake_interval=0.01)
        )
        await asyncio.sleep(0.05)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task


@pytest.mark.asyncio
async def test_status_writer_tick_cancellation_and_shield(mock_state: RuntimeState) -> None:
    from mcubridge.state.status import status_writer

    def _dummy_write(_p: ProtobufMessage) -> None:
        pass

    with patch("mcubridge.state.status._write_status_file", side_effect=_dummy_write):
        task = asyncio.create_task(status_writer(mock_state, interval=1))
        await asyncio.sleep(0.01)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task


def test_daemon_exception_group_with_unhandled_and_state_cleanup() -> None:
    from mcubridge.daemon import run_daemon

    # Exception group containing both handled and unhandled exception
    exc_grp = ExceptionGroup("mixed", [OSError("Handled error"), KeyError("Unhandled key error")])

    with patch("mcubridge.daemon.load_runtime_config"):
        with patch("mcubridge.daemon.configure_logging"):
            with patch("mcubridge.daemon.verify_crypto_integrity", return_value=True):
                with patch("mcubridge.daemon.create_runtime_state") as mock_create_state:
                    mock_st = MagicMock()
                    mock_create_state.return_value = mock_st
                    with patch("mcubridge.daemon.SerialTransport", side_effect=exc_grp):
                        with pytest.raises(ExceptionGroup):
                            run_daemon()
                        assert mock_st.cleanup.called


# ==========================================
# 25. Serial Correlation, SSL/CA Context & Handshake Transitions
# ==========================================


def test_serial_correlate_frame_debug_and_corrupt_ack_payload(
    test_config: RuntimeConfig, mock_state: RuntimeState
) -> None:
    from mcubridge.transport.serial import PendingCommand

    transport = SerialTransport(test_config, mock_state, None)

    # 1. Pending is None
    transport._correlate_frame(Status.ACK.value, b"")
    assert transport._current is None

    # 2. Pending already resolved
    transport._current = PendingCommand(
        command_id=Command.CMD_DIGITAL_WRITE.value,
        expected_resp_ids=[],
        success=True,
    )
    transport._correlate_frame(Status.ACK.value, b"")
    assert transport._current.success is True

    # 3. Pending with corrupted Protobuf ACK payload
    transport._current = PendingCommand(
        command_id=Command.CMD_DIGITAL_WRITE.value,
        expected_resp_ids=[],
    )
    transport._correlate_frame(Status.ACK.value, b"\xff\xff\xff\xff")
    assert transport._current.success is True


def test_structures_build_ssl_context_with_real_ca_and_resolve_properties(tmp_path: Path) -> None:
    from mcubridge.protocol.structures import _build_cached_ssl_context, resolve_cloud_context

    ca_file = tmp_path / "ca.crt"
    ca_file.write_text("dummy-ca-data")

    with patch("ssl.create_default_context"):
        ctx = _build_cached_ssl_context(
            cloud_cafile=str(ca_file),
            cloud_certfile="",
            cloud_keyfile="",
            cloud_tls_insecure=False,
        )
        assert ctx is not None

    # Test resolve_cloud_context with properties
    class DummyProps:
        ResponseTopic = "cloud/response/topic"
        CorrelationData = b"corr-token-123"

    class DummyCtx:
        properties = DummyProps()
        topic = "bridge/in/topic"

    msg = pb.CloudQueuedPublish(topic_name="bridge/default", payload=b"test-data")
    resolved = resolve_cloud_context(msg, DummyCtx())
    assert resolved.topic_name == "cloud/response/topic"
    assert resolved.correlation_data == b"corr-token-123"


def test_config_get_uci_config_import_error() -> None:
    import builtins
    from mcubridge.config.common import get_uci_config

    orig_import = builtins.__import__

    def _import_mock(name: str, *args: Any, **kwargs: Any) -> Any:
        if name == "uci":
            raise ImportError("No module named uci")
        return orig_import(name, *args, **kwargs)

    with patch("builtins.__import__", side_effect=_import_mock):
        cfg = get_uci_config()
        assert "serial_port" in cfg


@pytest.mark.asyncio
async def test_handshake_state_transition_from_sync_to_unsync_and_retry_stats(
    test_config: RuntimeConfig, mock_state: RuntimeState
) -> None:
    from mcubridge.services.handshake import HandshakeState, SerialHandshakeManager

    fsm = SerialHandshakeManager(
        config=test_config,
        state=mock_state,
        serial_timing=pb.HandshakeConfig(),
        send_frame=AsyncMock(return_value=True),
        enqueue_cloud=AsyncMock(),
        acknowledge_frame=AsyncMock(),
    )

    # 1. Transition from SYNCHRONIZED to UNSYNCHRONIZED
    fsm.fsm_state = HandshakeState.SYNCHRONIZED
    fsm._set_fsm_state(HandshakeState.UNSYNCHRONIZED)
    assert fsm.fsm_state == HandshakeState.UNSYNCHRONIZED

    # 2. Synchronize success debugging stats
    with patch.object(fsm, "_synchronize_attempt", new_callable=AsyncMock, return_value=True):
        ok = await fsm.synchronize()
        assert ok is True


def test_tls_session_ticket_persistence_memory_and_lmdb(tmp_path: Path) -> None:
    from mcubridge.protocol.structures import load_tls_session_ticket, save_tls_session_ticket
    from mcubridge.state.storage import LmdbCache

    # 1. None cache
    save_tls_session_ticket(None, "cloud.local", 8443, b"ticket-data")
    assert load_tls_session_ticket(None, "cloud.local", 8443) is None

    # 2. In-memory cache
    mem_cache = LmdbCache(":memory:")
    save_tls_session_ticket(mem_cache, "cloud.local", 8443, b"ticket-mem-123")
    ticket = load_tls_session_ticket(mem_cache, "cloud.local", 8443)
    assert ticket == b"ticket-mem-123"
    assert load_tls_session_ticket(mem_cache, "other.host", 8443) is None

    # 3. Disk-backed LMDB cache
    db_path = tmp_path / "tls_session_test"
    db_path.mkdir(parents=True, exist_ok=True)
    disk_cache = LmdbCache(str(db_path / "tls.db"))
    try:
        save_tls_session_ticket(disk_cache, "cloud.example.org", 8843, b"TLS13_TICKET_456")
        loaded = load_tls_session_ticket(disk_cache, "cloud.example.org", 8843)
        assert loaded == b"TLS13_TICKET_456"
        assert load_tls_session_ticket(disk_cache, "missing.host", 8843) is None
    finally:
        asyncio.run(disk_cache.close())


@pytest.mark.asyncio
async def test_connect_cloud_session_with_0rtt_session_ticket(
    test_config: RuntimeConfig, mock_state: RuntimeState
) -> None:
    from mcubridge.protocol.structures import save_tls_session_ticket, load_tls_session_ticket
    from mcubridge.state.storage import LmdbCache

    mock_state.tls_session_cache = LmdbCache(":memory:")
    save_tls_session_ticket(
        mock_state.tls_session_cache, test_config.cloud_host, test_config.cloud_port, b"EXISTING_TICKET"
    )

    serial = AsyncMock(spec=SerialTransport)
    svc = BridgeService(test_config, mock_state, serial)

    class MockStream:
        def __init__(self) -> None:
            self.send_message = AsyncMock()

        async def __aenter__(self) -> "MockStream":
            return self

        async def __aexit__(self, *args: Any) -> None:
            pass

        def __aiter__(self) -> Any:
            async def _gen() -> Any:
                if False:
                    yield None

            return _gen()

    mock_stub = MagicMock()
    mock_stub.Session.open.return_value = MockStream()

    with patch("mcubridge.services.runtime.Channel"):
        with patch("mcubridge.services.runtime.CloudBridgeStub", return_value=mock_stub):
            await svc.connect_cloud_session(tls_context=MagicMock())
            # Assert ticket was retained and updated
            saved_ticket = load_tls_session_ticket(
                mock_state.tls_session_cache, test_config.cloud_host, test_config.cloud_port
            )
            assert saved_ticket is not None
            assert len(saved_ticket) > 0
