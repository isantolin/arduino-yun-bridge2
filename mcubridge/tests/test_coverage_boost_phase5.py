"""Comprehensive unit test suite targeting uncovered branches across McuBridge components."""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable, Iterator
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest
from gateway import CloudBridgeService, ProtobufGateway
from hypothesis import given
from hypothesis import strategies as st
from mcubridge.config.settings import RuntimeConfig
from mcubridge.daemon import app as daemon_app
from mcubridge.daemon import cli as daemon_cli
from mcubridge.protocol import mcubridge_pb2 as pb
from mcubridge.protocol.protocol import Command, Status
from mcubridge.services.handshake import SerialHandshakeManager
from mcubridge.services.local_bridge import RPC_DISPATCH_TABLE
from mcubridge.services.runtime import BridgeService, LocalBridgeService, ProcessContext
from mcubridge.state.context import RuntimeState, create_runtime_state
from mcubridge.state.storage import LmdbDeque
from mcubridge.transport.serial import SerialTransport
from pytest_mock import MockerFixture


def _make_config() -> RuntimeConfig:
    return RuntimeConfig(
        topic_prefix="br",
        serial_port="/dev/null",
        serial_baud=115200,
        cloud_spool_dir="/tmp/spool_test",
        allow_non_tmp_paths=True,
    )


@pytest.fixture
def test_config(tmp_path: Path) -> RuntimeConfig:
    cfg = _make_config()
    cfg.file_system_root = str(tmp_path)
    return cfg


@pytest.fixture
def mock_state(test_config: RuntimeConfig) -> Iterator[RuntimeState]:
    state = create_runtime_state(test_config)
    yield state
    for res in (state.datastore_cache, state.mailbox_queue, state.mailbox_incoming_queue, state.tls_session_cache):
        env = getattr(res, "env", None)
        if env is not None:
            env.close()
            setattr(res, "env", None)


# ==========================================
# 2. LocalBridgeService & IPC Edge Paths
# ==========================================


@pytest.mark.asyncio
async def test_local_bridge_service_publish_timeout_and_oserror(
    test_config: RuntimeConfig, mock_state: RuntimeState, mocker: MockerFixture
) -> None:
    serial = AsyncMock(spec=SerialTransport)
    svc = BridgeService(test_config, mock_state, serial)
    local_svc = LocalBridgeService(svc)

    req_msg = pb.CloudQueuedPublish(
        topic_name="br/file/read",
        payload=b"test",
        correlation_data=b"corr-timeout-1",
    )
    mock_stream = AsyncMock()
    mock_stream.recv_message.return_value = req_msg

    mocker.patch.object(svc, "handle_request", new_callable=AsyncMock)
    _orig_timeout = asyncio.timeout

    def _quick_timeout(t: float) -> Any:
        return _orig_timeout(0.001)

    mocker.patch("mcubridge.services.runtime.asyncio.timeout", side_effect=_quick_timeout)
    await local_svc.Publish(mock_stream)
    assert mock_stream.send_message.called

    # 2. Simulate OSError during response write
    mock_stream.reset_mock()
    mock_stream.recv_message.return_value = req_msg
    mock_stream.send_message.side_effect = OSError("Socket broken")

    async def _handle_and_reply(req: pb.CloudQueuedPublish) -> None:
        if req.correlation_data in svc.ipc_requests:
            await svc.ipc_requests[req.correlation_data].put(pb.CloudQueuedPublish(topic_name="br/reply"))

    mocker.patch.object(svc, "handle_request", side_effect=_handle_and_reply)
    await local_svc.Publish(mock_stream)
    assert b"corr-timeout-1" not in svc.ipc_requests


@given(
    invalid_method=st.text(min_size=1, max_size=50).filter(lambda s: s not in RPC_DISPATCH_TABLE),
    payload=st.binary(max_size=256),
)
def test_local_bridge_service_execute_rpc_unknown_method(invalid_method: str, payload: bytes) -> None:
    async def _run() -> None:
        cfg = _make_config()
        state = create_runtime_state(cfg)
        try:
            serial = AsyncMock(spec=SerialTransport)
            svc = BridgeService(cfg, state, serial)
            local_svc = LocalBridgeService(svc)

            with pytest.raises(ValueError, match="Unknown RPC method"):
                await local_svc.execute_rpc(invalid_method, payload)
        finally:
            state.cleanup()

    asyncio.run(_run())


# ==========================================
# 3. Cloud Spooling & Protocol Edge Cases
# ==========================================


@pytest.mark.asyncio
async def test_flush_cloud_spool_corrupt_and_index_error(
    test_config: RuntimeConfig, mock_state: RuntimeState, mocker: MockerFixture
) -> None:
    serial = AsyncMock(spec=SerialTransport)
    svc = BridgeService(test_config, mock_state, serial)
    setattr(svc, "_cloud_stream", AsyncMock())

    mock_spool = MagicMock(spec=LmdbDeque)
    setattr(svc, "_cloud_spool", mock_spool)
    flush_spool: Callable[[], Awaitable[None]] = getattr(svc, "_flush_cloud_spool_locked")

    # Case 2: spool.peek raises IndexError
    svc.state.cloud_spool_degraded = False
    mock_spool.__len__.return_value = 1
    mock_spool.peek = AsyncMock(side_effect=IndexError("empty"))
    mock_spool.popleft = AsyncMock()
    mock_spool.vacuum = AsyncMock()
    await flush_spool()
    assert mock_spool.peek.called

    # Case 3: spool.peek returns corrupt data and popleft raises OSError
    mock_spool.__len__.side_effect = [1, 1, 0, 0]
    mock_spool.peek = AsyncMock(return_value=b"not-a-valid-protobuf")
    mock_spool.popleft = AsyncMock(side_effect=OSError("Disk failure"))
    await flush_spool()
    assert mock_spool.popleft.called

    # Case 4: spool.popleft raises IndexError after publish
    valid_msg = pb.CloudQueuedPublish(topic_name="br/t", payload=b"p")
    mock_spool.__len__.side_effect = [1, 0, 0]
    mock_spool.peek = AsyncMock(return_value=valid_msg.SerializeToString())
    mock_spool.popleft = AsyncMock(side_effect=IndexError("popped early"))
    mocker.patch.object(svc, "_publish_cloud_message", new_callable=AsyncMock, return_value=True)
    await flush_spool()
    assert mock_spool.peek.called


# ==========================================
# 4. Metrics & Telemetry Edge Branches
# ==========================================


@pytest.mark.asyncio
async def test_emit_bridge_snapshot_attribute_error(mock_state: RuntimeState, mocker: MockerFixture) -> None:
    enqueue = AsyncMock()
    mocker.patch.object(mock_state, "build_bridge_snapshot", side_effect=AttributeError("Missing attr"))
    import mcubridge.metrics as metrics_mod

    emit_snapshot: Callable[..., Awaitable[None]] = getattr(metrics_mod, "_emit_bridge_snapshot")
    await emit_snapshot(mock_state, enqueue, flavor="summary")
    assert enqueue.call_count == 0


# ==========================================
# 5. Transport & Handshake Error Paths
# ==========================================


@pytest.mark.asyncio
async def test_serial_transport_toggle_dtr_error(test_config: RuntimeConfig, mock_state: RuntimeState) -> None:
    transport = SerialTransport(test_config, mock_state, None)
    mock_serial = AsyncMock()
    mock_serial.set_modem_pins.side_effect = OSError("I/O error")
    transport.serial = mock_serial
    toggle_dtr_fn: Callable[[], Awaitable[None]] = getattr(transport, "_toggle_dtr")
    await toggle_dtr_fn()
    assert mock_serial.set_modem_pins.called


def test_serial_transport_switch_local_baudrate_error(test_config: RuntimeConfig, mock_state: RuntimeState) -> None:
    transport = SerialTransport(test_config, mock_state, None)
    mock_serial = MagicMock()
    mock_inner_serial = MagicMock()
    type(mock_inner_serial).baudrate = property(
        fget=lambda self: 115200,
        fset=MagicMock(side_effect=ValueError("Invalid baud")),
    )
    mock_serial.transport.serial = mock_inner_serial
    transport.serial = mock_serial
    switch_baud: Callable[[int], None] = getattr(transport, "_switch_local_baudrate")
    with pytest.raises(RuntimeError):
        switch_baud(99999999)


@pytest.mark.asyncio
async def test_serial_transport_send_failure_status_code(test_config: RuntimeConfig, mock_state: RuntimeState) -> None:
    transport = SerialTransport(test_config, mock_state, None)
    mock_serial = AsyncMock()
    mock_serial.is_open = True
    transport.serial = mock_serial

    send_task = asyncio.create_task(transport.send(Command.CMD_GET_VERSION.value, b""))
    await asyncio.sleep(0.01)
    correlate_fn: Callable[[int, bytes], None] = getattr(transport, "_correlate_frame")
    correlate_fn(Status.ERROR.value, b"")
    res = await send_task
    assert res is False


@given(
    nonce=st.binary(min_size=1, max_size=32),
)
def test_handshake_calculate_tag_empty_secret(nonce: bytes) -> None:
    assert SerialHandshakeManager.calculate_handshake_tag(None, nonce) == b""
    assert SerialHandshakeManager.calculate_handshake_tag(b"", nonce) == b""


# ==========================================
# 7. Daemon Entrypoint & Exception Handling
# ==========================================


def test_daemon_app_version() -> None:
    from typer.testing import CliRunner

    runner = CliRunner()
    res = runner.invoke(daemon_cli, ["--help"])
    assert res.exit_code == 0
    assert "Arduino MCU Bridge" in res.output or "daemon" in res.output.lower()

    with pytest.raises(SystemExit) as exc_info:
        daemon_app(["--help"])
    assert exc_info.value.code == 0


# ==========================================
# 8. Gateway & Cloud Dispatch
# ==========================================


@pytest.mark.asyncio
async def test_gateway_session_cancelled(mocker: MockerFixture) -> None:
    gw = ProtobufGateway(use_tls=False)
    svc = CloudBridgeService(gw)

    mock_stream = AsyncMock()
    mock_stream.__aiter__.side_effect = asyncio.CancelledError()

    mocker.patch("gateway.extract_peer_identity", return_value=("test-dev", True))

    with pytest.raises(asyncio.CancelledError):
        await svc.Session(mock_stream)


@given(
    free_bytes=st.integers(0, 1024),
    data=st.binary(min_size=1, max_size=2048),
)
def test_runtime_write_with_quota_property(
    tmp_path_factory: pytest.TempPathFactory,
    free_bytes: int,
    data: bytes,
) -> None:
    async def _run() -> None:
        import psutil

        tmp_dir = tmp_path_factory.mktemp("quota")
        config = RuntimeConfig(
            file_system_root=str(tmp_dir),
            cloud_spool_dir=str(tmp_dir / "spool"),
            allow_non_tmp_paths=True,
        )
        state = create_runtime_state(config)
        orig_usage = psutil.disk_usage
        try:
            serial = AsyncMock(spec=SerialTransport)
            svc = BridgeService(config, state, serial)
            target_file = tmp_dir / "quota_test.bin"

            write_quota_fn: Callable[..., Awaitable[bool]] = getattr(svc, "_write_with_quota")

            initial_rejections = svc.state.file_storage_limit_rejections
            mock_usage = MagicMock()
            mock_usage.side_effect = None
            mock_usage.return_value = MagicMock(free=free_bytes, used=100, total=100 + free_bytes)
            psutil.disk_usage = mock_usage

            res = await write_quota_fn(target_file, data)
            if len(data) > free_bytes:
                assert res is False
                assert svc.state.file_storage_limit_rejections == initial_rejections + 1
            else:
                assert res is True
                assert target_file.read_bytes() == data

            # Error path fallback
            mock_usage.return_value = None
            mock_usage.side_effect = OSError("Stat failure")
            res_fallback = await write_quota_fn(target_file, data)
            assert res_fallback is True
            assert target_file.read_bytes() == data
        finally:
            psutil.disk_usage = orig_usage
            state.cleanup()

    asyncio.run(_run())


@pytest.mark.asyncio
async def test_runtime_terminate_process_escalation(
    test_config: RuntimeConfig, mock_state: RuntimeState, mocker: MockerFixture
) -> None:
    serial = AsyncMock(spec=SerialTransport)
    svc = BridgeService(test_config, mock_state, serial)

    mock_handle = AsyncMock()
    mock_handle.returncode = None
    mock_handle.pid = 12345

    ctx = ProcessContext(mock_handle)

    mock_term = mocker.patch("mcubridge.services.runtime.terminate_pid_tree")
    term_proc_fn: Callable[..., Awaitable[int]] = getattr(svc, "_terminate_process")
    code = await term_proc_fn(12345, ctx, grace_period=0.5)
    mock_term.assert_called_once_with(12345, timeout=0.5)
    assert code == -1
    assert ctx.fsm.current_state_value in {"terminating", "exited"}


@pytest.mark.asyncio
async def test_runtime_flush_console_queue_send_failed(test_config: RuntimeConfig, mock_state: RuntimeState) -> None:
    serial = AsyncMock(spec=SerialTransport)
    serial.send.return_value = False
    svc = BridgeService(test_config, mock_state, serial)

    svc.state.console_to_mcu_queue.append(b"console payload")
    flush_console_fn: Callable[[], Awaitable[None]] = getattr(svc, "_flush_console_queue")
    await flush_console_fn()
    assert len(svc.state.console_to_mcu_queue) == 1


@given(
    action=st.sampled_from(["read", "write", "mode", "toggle"]),
    topic_str=st.text(alphabet="abcdefghijklmnopqrstuvwxyz0123456789_", min_size=1, max_size=20),
)
def test_runtime_reject_cloud_topic_variants(
    tmp_path_factory: pytest.TempPathFactory, action: str, topic_str: str
) -> None:
    async def _run() -> None:
        tmp_dir = tmp_path_factory.mktemp("reject_cloud")
        config = RuntimeConfig(
            file_system_root=str(tmp_dir),
            cloud_spool_dir=str(tmp_dir / "spool"),
            allow_non_tmp_paths=True,
        )
        state = create_runtime_state(config)
        try:
            serial = AsyncMock(spec=SerialTransport)
            svc = BridgeService(config, state, serial)
            mock_enqueue = AsyncMock()
            svc.enqueue_cloud = mock_enqueue
            from mcubridge.protocol.protocol import Topic

            reject_cloud_fn: Callable[..., Awaitable[None]] = getattr(svc, "_reject_cloud")
            await reject_cloud_fn(pb.CloudQueuedPublish(), Topic.DIGITAL, action)
            assert mock_enqueue.called

            mock_enqueue.reset_mock()
            await reject_cloud_fn(pb.CloudQueuedPublish(), topic_str, action)
            assert mock_enqueue.called
        finally:
            state.cleanup()

    asyncio.run(_run())


@pytest.mark.asyncio
async def test_runtime_cloud_spool_trimming_and_drop(test_config: RuntimeConfig, mock_state: RuntimeState) -> None:
    serial = AsyncMock(spec=SerialTransport)
    svc = BridgeService(test_config, mock_state, serial)

    svc.state.cloud_queue_limit = 2
    mock_spool = MagicMock(spec=LmdbDeque)
    setattr(svc, "_cloud_spool", mock_spool)
    spool_msg_fn: Callable[..., Awaitable[bool]] = getattr(svc, "_spool_cloud_message_locked")

    # Simulate atomic spool append trimming on first call and no trim on second
    mock_spool.__len__.return_value = 1
    mock_spool.popleft = AsyncMock(return_value=b"old")
    mock_spool.append = AsyncMock(side_effect=[1, 0])

    msg = pb.CloudQueuedPublish(topic_name="br/test", payload=b"data")
    res = await spool_msg_fn(msg)
    assert res is True
    assert svc.state.cloud_spool_dropped_limit == 1
    assert svc.state.cloud_spool_trim_events == 1

    # Second call where queue is below limit: trim_events must NOT increment even though dropped_limit > 0
    mock_spool.__len__.side_effect = None
    mock_spool.__len__.return_value = 1
    res2 = await spool_msg_fn(msg)
    assert res2 is True
    assert svc.state.cloud_spool_dropped_limit == 1
    assert svc.state.cloud_spool_trim_events == 1


@pytest.mark.asyncio
async def test_runtime_cleanup_and_lifecycle(
    test_config: RuntimeConfig, mock_state: RuntimeState, tmp_path: Path
) -> None:
    serial = AsyncMock(spec=SerialTransport)
    svc = BridgeService(test_config, mock_state, serial)
    svc.ubus_service = MagicMock()
    svc.cleanup()
    assert svc.serial is None
    assert svc.ubus_service.stop.called
