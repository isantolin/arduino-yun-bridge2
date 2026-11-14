"""Tests for runtime service lifecycle and basic transport integration."""

from __future__ import annotations

import asyncio
import operator
import time
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import Any, cast
from unittest.mock import AsyncMock, MagicMock, patch

import lmdb
import pw_hdlc.decode
import pw_rpc.packets
import pytest
from hypothesis import given
from hypothesis import strategies as st
from mcubridge.config.settings import RuntimeConfig
from mcubridge.protocol import cloud_gateway_pb2, config_schema_pb2, mcu_wire_pb2
from mcubridge.protocol import protocol
from mcubridge.protocol.gateway import gateway_control_pb2
from mcubridge.protocol.protocol import Status

from mcubridge.services.handshake import SerialHandshakeFatal
from mcubridge.services.runtime import BridgeService
from mcubridge.state.context import ProcessContext, RuntimeState, create_runtime_state

from mcubridge.state.storage import LmdbDeque
from mcubridge.transport.serial import SerialTransport
from pytest_mock import MockerFixture


def _make_config() -> RuntimeConfig:
    fs_root = f".tmp_tests/fs-{time.time_ns()}"
    spool_dir = f".tmp_tests/spool-{time.time_ns()}"
    return RuntimeConfig(
        serial_port="/dev/test0",
        serial_baud=protocol.DEFAULT_BAUDRATE,
        serial_safe_baud=protocol.DEFAULT_SAFE_BAUDRATE,
        allowed_commands=("echo", "ls"),
        serial_shared_secret=b"testshared",
        file_system_root=fs_root,
        cloud_spool_dir=spool_dir,
        allow_non_tmp_paths=True,
    )


async def test_send_frame_via_transport() -> None:
    service = None
    config = _make_config()
    state = create_runtime_state(config)
    try:
        mock_serial = AsyncMock(spec=SerialTransport)
        mock_serial.send.return_value = True
        service = BridgeService(config, state, mock_serial)

        assert service.serial is not None
        ok = await service.serial.send("rpc.pb.SystemService/GetVersion", b"x")
        assert ok
        mock_serial.send.assert_called_once()
    finally:
        if service is not None:
            service.cleanup()
        else:
            state.cleanup()


async def test_handle_mcu_frame_pre_sync_denied() -> None:
    service = None
    config = _make_config()
    state = create_runtime_state(config)
    try:
        mock_serial = AsyncMock(spec=SerialTransport)
        mock_serial.send_raw.return_value = True
        service = BridgeService(config, state, mock_serial)

        # Before sync, MCU frames other than handshake are ignored/denied
        await service.handle_mcu_frame("rpc.pb.SystemService/GetVersion", 1, b"")
        mock_serial.send_raw.assert_not_called()
    finally:
        if service is not None:
            service.cleanup()
        else:
            state.cleanup()


async def test_handle_mcu_frame_dispatches_by_pigweed_service_and_method() -> None:
    config = _make_config()
    state = create_runtime_state(config)
    service = None
    try:
        mock_serial = AsyncMock(spec=SerialTransport)
        service = BridgeService(config, state, mock_serial)
        state.state = "synchronized"

        spec = protocol.get_operation("rpc.pb.DataStoreService/Put")
        assert spec is not None
        assert spec.service_id is not None
        assert spec.method_id is not None
        handler = AsyncMock()
        service.mcu_registry[(spec.service_id, spec.method_id)] = handler
        payload = mcu_wire_pb2.DatastorePut(key="sample", value=b"value")

        await service.handle_mcu_frame((spec.service_id, spec.method_id), 17, payload)

        handler.assert_awaited_once_with(17, payload)
        mock_serial.send.assert_not_called()
    finally:
        if service is not None:
            service.cleanup()
        else:
            state.cleanup()


async def test_set_mcu_paused_toggles_tx_event() -> None:
    service = None
    config = _make_config()
    state = create_runtime_state(config)
    try:
        mock_serial = AsyncMock(spec=SerialTransport)
        service = BridgeService(config, state, mock_serial)
        state.state = "synchronized"

        await service.set_mcu_paused(True)
        assert state.mcu_is_paused
        assert not state.serial_tx_allowed.is_set()

        await service.set_mcu_paused(False)
        assert not state.mcu_is_paused
        assert state.serial_tx_allowed.is_set()
    finally:
        if service is not None:
            service.cleanup()
        else:
            state.cleanup()


async def test_handle_cloud_console_queues_and_flushes() -> None:
    service = None
    config = _make_config()
    state = create_runtime_state(config)
    try:
        mock_serial = AsyncMock(spec=SerialTransport)
        mock_serial.send.return_value = True
        service = BridgeService(config, state, mock_serial)
        state.state = "synchronized"
        state.link_sync_event.set()
        state.serial_tx_allowed.set()

        service.state.console_to_mcu_queue.append(b"hello")
        await service.flush_console_queue()

        mock_serial.send.assert_called()
    finally:
        if service is not None:
            service.cleanup()
        else:
            state.cleanup()


async def test_enqueue_cloud_spools_until_client_recovers() -> None:
    service = None
    config = _make_config()
    state = create_runtime_state(config)
    try:
        service = BridgeService(config, state, AsyncMock(spec=SerialTransport))
        message = cloud_gateway_pb2.CloudQueuedPublish(topic_name="br/system/status", payload=b"payload")

        await service.enqueue_cloud(message)

        assert state.cloud_spool_pending_messages == 1

        mock_client = MagicMock()
        mock_writer = MagicMock()
        mock_writer.drain = AsyncMock()
        service.cloud_rpc_client = mock_client
        service.cloud_writer = mock_writer
        service.send_cloud_telemetry = AsyncMock(return_value=True)
        await service.flush_cloud_spool()

        service.send_cloud_telemetry.assert_awaited_once()
        assert state.cloud_spool_pending_messages == 0
    finally:
        if service is not None:
            service.cleanup()
        else:
            state.cleanup()


async def test_handle_cloud_pin_overflow_reports_error() -> None:
    service = None
    config = _make_config()
    state = create_runtime_state(config)
    try:
        mock_serial = AsyncMock(spec=SerialTransport)
        mock_serial.send.side_effect = RuntimeError("serial-transport-error")
        service = BridgeService(config, state, mock_serial)
        state.state = "synchronized"
        state.link_sync_event.set()

        captured: list[cloud_gateway_pb2.CloudQueuedPublish] = []

        async def capture_enqueue(message: cloud_gateway_pb2.CloudQueuedPublish) -> None:
            captured.append(message)

        service.cloud_publisher = capture_enqueue

        message = cloud_gateway_pb2.CloudQueuedPublish(
            topic_name=f"{config.topic_prefix}/req/rpc.pb.GpioService/DigitalRead",
            payload=mcu_wire_pb2.PinRead(pin=13).SerializeToString(),
        )

        await service.handle_request(message)

        assert captured
        assert b"error:serial-transport-error" in captured[0].payload
    finally:
        if service is not None:
            service.cleanup()
        else:
            state.cleanup()


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

    res = await svc.process_poll(8888)
    assert res.finished is False
    assert res.stdout_truncated is True


async def test_connect_cloud_session(mock_bridge_service: BridgeService, mocker: MockerFixture) -> None:
    svc = mock_bridge_service
    connect_http2 = mocker.spy(svc.state.cloud_fsm, "connect_http2")

    mock_reader = AsyncMock()
    mock_reader.read = AsyncMock(return_value=b"")
    mock_writer = MagicMock()
    mock_writer.is_closing = MagicMock(return_value=False)
    mock_writer.write = MagicMock()
    mock_writer.drain = AsyncMock()
    mock_writer.close = MagicMock()
    mock_writer.wait_closed = AsyncMock()

    mocker.patch("asyncio.open_connection", return_value=(mock_reader, mock_writer))

    with pytest.raises(ConnectionError, match="Cloud Gateway connection closed by peer"):
        await svc.connect_cloud_session(None)

    connect_http2.assert_called_once()
    assert svc.state.cloud_fsm.spooling_degraded.is_active


async def test_process_terminate_sigkill_escalation(mocker: MockerFixture) -> None:
    mock_ctx = MagicMock()
    mock_ctx.handle.returncode = None
    mock_ctx.handle.pid = 999999

    mock_term = mocker.patch("mcubridge.services.runtime.terminate_pid_tree")
    terminate_proc: Callable[..., Awaitable[int]] = BridgeService.terminate_process
    code = await terminate_proc(MagicMock(), 999999, mock_ctx, grace_period=0.5)
    mock_term.assert_called_once_with(999999, timeout=0.5)
    assert code == -1


async def test_runtime_service_run_and_teardown_exceptions(mock_bridge_service: BridgeService) -> None:
    service = mock_bridge_service
    state = service.state
    service.config.bridge_summary_interval = 1.0
    service.config.watchdog_enabled = True
    service.config.watchdog_interval = 1.0

    mock_spool = AsyncMock(spec=LmdbDeque)
    mock_spool.close.side_effect = OSError("spool close error")
    service.cloud_spool = mock_spool

    mock_cache = AsyncMock()
    mock_cache.close.side_effect = lmdb.Error("db error")
    state.datastore_cache = mock_cache

    run_task = asyncio.create_task(service.run())
    await asyncio.sleep(0.05)
    run_task.cancel()

    await run_task
    assert run_task.done()
    assert service.cloud_spool is None


async def test_runtime_run_cloud_disabled(mock_bridge_service: BridgeService) -> None:
    service = mock_bridge_service
    service.config.cloud_enabled = False
    await service.run_cloud()
    assert service.state.cloud_fsm.disabled.is_active


@given(
    key=st.text(alphabet="abcdefghijklmnopqrstuvwxyz0123456789_", min_size=1, max_size=24),
    value=st.binary(min_size=1, max_size=128),
)
def test_runtime_handle_datastore_flavors(tmp_path_factory: pytest.TempPathFactory, key: str, value: bytes) -> None:
    async def _run() -> None:
        p = tmp_path_factory.mktemp("ds_flavors")
        cfg = RuntimeConfig(
            file_system_root=str(p),
            allow_non_tmp_paths=True,
            cloud_spool_dir=str(p / "spool"),
            topic_prefix="test/br",
        )
        state = create_runtime_state(cfg)
        mock_serial = AsyncMock(spec=SerialTransport)
        mock_serial.send = AsyncMock(return_value=True)
        service = BridgeService(cfg, state, mock_serial)

        inbound_put = cloud_gateway_pb2.CloudQueuedPublish(
            topic_name=f"{cfg.topic_prefix}/req/rpc.pb.DataStoreService/Put",
            payload=mcu_wire_pb2.DatastorePut(key=key, value=value).SerializeToString(),
        )
        await service.handle_request(inbound_put)
        assert state.datastore_cache is not None
        assert await state.datastore_cache.get(key) == value

        mock_enqueue = AsyncMock()
        service.cloud_publisher = mock_enqueue
        inbound_get = cloud_gateway_pb2.CloudQueuedPublish(
            topic_name=f"{cfg.topic_prefix}/req/rpc.pb.DataStoreService/Get",
            payload=mcu_wire_pb2.DatastoreGet(key=key).SerializeToString(),
        )
        await service.handle_request(inbound_get)
        assert mock_enqueue.await_count == 1

        mock_enqueue.reset_mock()
        inbound_get_miss = cloud_gateway_pb2.CloudQueuedPublish(
            topic_name=f"{cfg.topic_prefix}/req/rpc.pb.DataStoreService/Get",
            payload=mcu_wire_pb2.DatastoreGet(key=f"non_existing_{key}").SerializeToString(),
        )
        await service.handle_request(inbound_get_miss)
        assert mock_enqueue.await_count == 1

        state.cleanup()

    asyncio.run(_run())


async def test_runtime_handle_mcu_status_binary_undecodable(
    mock_bridge_service: BridgeService,
) -> None:
    service = mock_bridge_service
    mock_enqueue = AsyncMock()
    service.cloud_publisher = mock_enqueue
    handle_mcu_status: Callable[..., Awaitable[None]] = service.handle_mcu_status
    await handle_mcu_status(Status.DEADLINE_EXCEEDED, 1, b"\xff\xfe\xfd\x80")
    assert mock_enqueue.await_count == 1
    await handle_mcu_status(Status.INTERNAL, 2, cast(Any, 12345))
    assert mock_enqueue.await_count == 2


@given(
    major=st.integers(0, 10),
    minor=st.integers(0, 50),
    patch_ver=st.integers(0, 100),
)
def test_runtime_request_mcu_version_and_system_version(
    tmp_path_factory: pytest.TempPathFactory,
    major: int,
    minor: int,
    patch_ver: int,
) -> None:
    async def _run() -> None:
        p = tmp_path_factory.mktemp("ver_prop")
        cfg = RuntimeConfig(file_system_root=str(p), allow_non_tmp_paths=True, topic_prefix="test/br")
        state = create_runtime_state(cfg)
        mock_serial = AsyncMock(spec=SerialTransport)
        service = BridgeService(cfg, state, mock_serial)

        v_resp = mcu_wire_pb2.VersionResponse(major=major, minor=minor, patch=patch_ver)
        mock_serial.send = AsyncMock(return_value=v_resp)
        inbound = cloud_gateway_pb2.CloudQueuedPublish(
            topic_name=f"{cfg.topic_prefix}/req/rpc.pb.SystemService/GetVersion", payload=b""
        )

        req_mcu_version: Callable[[cloud_gateway_pb2.CloudQueuedPublish], Awaitable[bool]] = service.request_mcu_version
        res = await req_mcu_version(inbound)
        assert res is True
        assert state.mcu_version == (major, minor, patch_ver)

        await service.handle_request(inbound)
        state.cleanup()

    asyncio.run(_run())


@given(
    pin=st.integers(0, 32),
    analog_val=st.integers(0, 255),
)
def test_runtime_pin_analog_and_invalid_digits(
    tmp_path_factory: pytest.TempPathFactory, pin: int, analog_val: int
) -> None:
    async def _run() -> None:
        p = tmp_path_factory.mktemp("pin_analog")
        cfg = RuntimeConfig(file_system_root=str(p), allow_non_tmp_paths=True, topic_prefix="test/br")
        state = create_runtime_state(cfg)
        state.link_sync_event.set()
        mock_serial = AsyncMock(spec=SerialTransport)
        mock_serial.send = AsyncMock(return_value=True)
        service = BridgeService(cfg, state, mock_serial)

        inbound_aw = cloud_gateway_pb2.CloudQueuedPublish(
            topic_name=f"{cfg.topic_prefix}/req/rpc.pb.GpioService/AnalogWrite",
            payload=mcu_wire_pb2.AnalogWrite(pin=pin, value=analog_val).SerializeToString(),
        )
        await service.handle_request(inbound_aw)
        mock_serial.send.assert_called_with(
            "rpc.pb.GpioService/AnalogWrite", mcu_wire_pb2.AnalogWrite(pin=pin, value=analog_val)
        )

        inbound_dw = cloud_gateway_pb2.CloudQueuedPublish(
            topic_name=f"{cfg.topic_prefix}/req/rpc.pb.GpioService/DigitalWrite",
            payload=mcu_wire_pb2.DigitalWrite(pin=pin, value=0).SerializeToString(),
        )
        await service.handle_request(inbound_dw)
        mock_serial.send.assert_called_with(
            "rpc.pb.GpioService/DigitalWrite", mcu_wire_pb2.DigitalWrite(pin=pin, value=0)
        )

        state.cleanup()

    asyncio.run(_run())


async def test_flush_cloud_spool_corrupt_and_index_error(
    mock_bridge_service: BridgeService,
) -> None:
    svc = mock_bridge_service
    svc.cloud_rpc_client = MagicMock()
    mock_writer = MagicMock()
    mock_writer.drain = AsyncMock()
    svc.cloud_writer = mock_writer
    svc.send_cloud_telemetry = AsyncMock(return_value=True)

    mock_spool = MagicMock(spec=LmdbDeque)
    svc.cloud_spool = mock_spool
    flush_spool: Callable[[], Awaitable[None]] = svc.flush_cloud_spool_locked

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
    mock_spool.popleft.assert_awaited_once()
    assert svc.state.cloud_spool_failure_reason == "Disk failure"
    assert svc.state.cloud_spool_corrupt_dropped == 0

    # Case 4: spool.popleft raises IndexError after publish
    valid_msg = cloud_gateway_pb2.CloudQueuedPublish(topic_name="br/t", payload=b"p")
    mock_spool.__len__.side_effect = [1, 0, 0]
    mock_spool.peek = AsyncMock(return_value=valid_msg.SerializeToString())
    mock_spool.popleft = AsyncMock(side_effect=IndexError("popped early"))
    await flush_spool()
    assert mock_spool.peek.called


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

            write_quota_fn: Callable[..., Awaitable[bool]] = svc.write_with_quota

            initial_rejections = svc.state.file_storage_limit_rejections
            mock_usage = MagicMock()
            mock_usage.side_effect = None
            mock_usage.return_value = MagicMock(free=free_bytes, used=100, total=100 + free_bytes)
            psutil.disk_usage = mock_usage

            res = await write_quota_fn(target_file, data)
            if len(data) > free_bytes:
                assert not res
                assert svc.state.file_storage_limit_rejections == initial_rejections + 1
            else:
                assert res
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


async def test_runtime_terminate_process_escalation(mock_bridge_service: BridgeService, mocker: MockerFixture) -> None:
    svc = mock_bridge_service

    mock_handle = AsyncMock()
    mock_handle.returncode = None
    mock_handle.pid = 12345

    ctx = ProcessContext(mock_handle)

    mock_term = mocker.patch("mcubridge.services.runtime.terminate_pid_tree")
    term_proc_fn: Callable[..., Awaitable[int]] = svc.terminate_process
    code = await term_proc_fn(12345, ctx, grace_period=0.5)
    mock_term.assert_called_once_with(12345, timeout=0.5)
    assert code == -1
    assert ctx.fsm.current_state_value in {"terminating", "exited"}


async def test_runtime_flush_console_queue_send_failed(
    mock_bridge_service: BridgeService, mock_serial: AsyncMock
) -> None:
    mock_serial.send.return_value = False
    svc = mock_bridge_service

    svc.state.console_to_mcu_queue.append(b"console payload")
    flush_console_fn: Callable[[], Awaitable[None]] = svc.flush_console_queue
    await flush_console_fn()
    assert len(svc.state.console_to_mcu_queue) == 1


@given(
    action=st.sampled_from(["read", "write", "mode", "toggle"]),
    topic_str=st.text(alphabet="abcdefghijklmnopqrstuvwxyz0123456789_", min_size=1, max_size=20),
)
def test_runtime_reject_cloud_topic_variants(action: str, topic_str: str) -> None:
    async def _run() -> None:
        svc = MagicMock(spec=BridgeService)
        svc.enqueue_cloud_status_report = AsyncMock()

        reject_cloud_fn: Callable[..., Awaitable[None]] = BridgeService.reject_cloud
        await reject_cloud_fn(svc, cloud_gateway_pb2.CloudQueuedPublish(), "rpc.pb.GpioService/DigitalWrite", action)
        svc.enqueue_cloud_status_report.assert_awaited_once()

        svc.enqueue_cloud_status_report.reset_mock()
        await reject_cloud_fn(svc, cloud_gateway_pb2.CloudQueuedPublish(), topic_str, action)
        svc.enqueue_cloud_status_report.assert_awaited_once()

    asyncio.run(_run())


async def test_runtime_cloud_spool_trimming_and_drop(mock_bridge_service: BridgeService) -> None:
    svc = mock_bridge_service

    svc.state.cloud_queue_limit = 2
    mock_spool = MagicMock(spec=LmdbDeque)
    svc.cloud_spool = mock_spool
    spool_msg_fn: Callable[..., Awaitable[bool]] = svc.spool_cloud_message_locked

    # Simulate atomic spool append trimming on first call and no trim on second
    mock_spool.__len__.return_value = 1
    mock_spool.popleft = AsyncMock(return_value=b"old")
    mock_spool.append = AsyncMock(side_effect=[1, 0])

    msg = cloud_gateway_pb2.CloudQueuedPublish(topic_name="br/test", payload=b"data")
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


async def test_runtime_cleanup_and_lifecycle(
    mock_bridge_service: BridgeService,
) -> None:
    svc = mock_bridge_service
    svc.ubus_service = MagicMock()
    svc.cleanup()
    assert svc.serial is None
    assert svc.ubus_service.stop.called


async def test_runtime_file_dispatch_local_methods_none_path(
    runtime_config: RuntimeConfig, mock_bridge_service: BridgeService, mock_serial: AsyncMock
) -> None:
    svc = mock_bridge_service
    req = cloud_gateway_pb2.CloudQueuedPublish(
        topic_name=f"{runtime_config.topic_prefix}/req/fs.read",
        payload=cloud_gateway_pb2.FileRead(path="../../secret").SerializeToString(),
    )
    await svc.handle_request(req)
    assert mock_serial.send.call_count == 0


async def test_runtime_handle_mcu_frame_branches(mock_bridge_service: BridgeService, mock_serial: AsyncMock) -> None:
    svc = mock_bridge_service

    svc.serial = None
    await svc.handle_mcu_frame("rpc.pb.SystemService/GetVersion", 1, b"")
    assert not mock_serial.send.called

    svc.serial = mock_serial
    svc.state.connection_fsm.synchronize()
    unknown_before = svc.state.unknown_command_count
    await svc.handle_mcu_frame(9999, 1, b"")
    # A bare integer key carries no service/method, so no call can be completed; it is only counted.
    assert svc.state.unknown_command_count == unknown_before + 1
    assert svc.state.unknown_command_last_id == 9999
    mock_serial.send_status.assert_not_awaited()

    # Test synchronous (non-awaitable) handler path
    called_sync: list[int] = []

    def _sync_handler(_seq: int, _p: Any) -> dict[str, str]:
        called_sync.append(_seq)
        return {"status": "ok"}

    svc.mcu_registry["rpc.pb.SystemService/GetCapabilities"] = _sync_handler
    await svc.handle_mcu_frame("rpc.pb.SystemService/GetCapabilities", 2, b"")
    assert called_sync == [2]

    # Test handler returning False
    called_async = AsyncMock(return_value=False)
    svc.mcu_registry["rpc.pb.ProcessService/Kill"] = called_async
    await svc.handle_mcu_frame("rpc.pb.ProcessService/Kill", 3, mcu_wire_pb2.ProcessKill(pid=123).SerializeToString())
    called_async.assert_awaited_once()


async def test_runtime_handle_request_route_none_and_inbound_props(
    mock_bridge_service: BridgeService, mock_serial: AsyncMock
) -> None:
    svc = mock_bridge_service

    await svc.handle_request(cloud_gateway_pb2.CloudQueuedPublish(topic_name="unmatched/topic", payload=b""))
    assert not mock_serial.send.called

    class InboundProps:
        ResponseTopic = "cloud/resp"
        CorrelationData = b"corr456"

    class InboundObj:
        properties = InboundProps()
        topic = "bridge/unmatched"
        payload = b"testpayload"

    await svc.handle_request(InboundObj())
    assert not mock_serial.send.called


async def test_runtime_handle_console_empty_payload(mock_bridge_service: BridgeService, mock_serial: AsyncMock) -> None:
    svc = mock_bridge_service
    await svc.handle_request(cloud_gateway_pb2.CloudQueuedPublish(topic_name="bridge/req/ConsoleWrite", payload=b""))
    assert not mock_serial.send.called


async def test_runtime_handle_datastore_branches(mock_bridge_service: BridgeService, mock_serial: AsyncMock) -> None:
    svc = mock_bridge_service

    await svc.handle_request(
        cloud_gateway_pb2.CloudQueuedPublish(
            topic_name="bridge/req/rpc.pb.DataStoreService/Put",
            payload=mcu_wire_pb2.DatastorePut(key="mykey", value=b"x" * 600).SerializeToString(),
        )
    )
    assert not mock_serial.send.called

    await svc.handle_request(
        cloud_gateway_pb2.CloudQueuedPublish(
            topic_name="bridge/req/rpc.pb.DataStoreService/Get",
            payload=mcu_wire_pb2.DatastoreGet(key="missingkey").SerializeToString(),
        )
    )
    assert not mock_serial.send.called

    await svc.handle_request(
        cloud_gateway_pb2.CloudQueuedPublish(
            topic_name="bridge/req/unknown_act",
            payload=b"",
        )
    )
    assert not mock_serial.send.called


async def test_runtime_handle_mailbox_unknown_identifier(
    mock_bridge_service: BridgeService, mock_serial: AsyncMock
) -> None:
    svc = mock_bridge_service
    await svc.handle_request(
        cloud_gateway_pb2.CloudQueuedPublish(
            topic_name="bridge/req/mailbox.unknown",
            payload=b"test",
        )
    )
    assert not mock_serial.send.called


async def test_runtime_handle_file_unhandled_action_and_failed_writes(
    mock_bridge_service: BridgeService, mock_serial: AsyncMock, tmp_path: Path
) -> None:
    svc = mock_bridge_service

    await svc.handle_request(
        cloud_gateway_pb2.CloudQueuedPublish(
            topic_name="bridge/req/file.custom_act",
            payload=b"data",
        )
    )

    mock_serial.transfer_write = AsyncMock()
    res_mcu_w = await svc.file_write("mcu/eeprom", b"data")
    mock_serial.transfer_write.assert_awaited_once_with(0x4D454741, b"data")
    assert res_mcu_w.status == "ok"

    mock_serial.transfer_write = AsyncMock(side_effect=RuntimeError("transfer failed"))
    res_mcu_w_failed = await svc.file_write("mcu/eeprom", b"data")
    assert res_mcu_w_failed.status == "error"

    mock_serial.transfer_write = AsyncMock()
    res_unknown_write = await svc.file_write("mcu/unknown", b"data")
    assert res_unknown_write.status == "error"
    mock_serial.transfer_write.assert_not_awaited()

    svc.serial = None
    res_mcu_rm = await svc.file_remove(mcu_wire_pb2.FileRemove(path="/mcu/test.txt"))
    assert res_mcu_rm.status == "error"
    svc.serial = mock_serial

    res_loc_w = await svc.file_write(mcu_wire_pb2.FileWrite(path="../outside.txt", data=b"data"))
    assert res_loc_w.status == "error"

    real_file = tmp_path / "real.txt"
    real_file.write_text("hello")
    svc.config.file_system_root = str(tmp_path)
    res_loc_rm = await svc.file_remove(mcu_wire_pb2.FileRemove(path="real.txt"))
    assert not real_file.exists()
    assert res_loc_rm.status == "ok"


async def test_runtime_handle_shell_branches(mock_bridge_service: BridgeService, mock_serial: AsyncMock) -> None:
    svc = mock_bridge_service

    await svc.handle_request(
        cloud_gateway_pb2.CloudQueuedPublish(
            topic_name="bridge/req/shell.unknown_act",
            payload=b"",
        )
    )
    assert not mock_serial.send.called

    proto_cmd = mcu_wire_pb2.ProcessRunAsync(command="echo hello").SerializeToString()
    await svc.handle_request(
        cloud_gateway_pb2.CloudQueuedPublish(
            topic_name="bridge/req/rpc.pb.ProcessService/RunAsync",
            payload=proto_cmd,
        )
    )

    await svc.process_kill(99999)
    assert 99999 not in svc.state.running_processes


async def test_runtime_handle_spi_and_pin_branches(mock_bridge_service: BridgeService, mock_serial: AsyncMock) -> None:
    svc = mock_bridge_service

    await svc.handle_request(
        cloud_gateway_pb2.CloudQueuedPublish(
            topic_name="bridge/req/rpc.pb.SpiService/Transfer",
            payload=b"",
        )
    )
    assert not mock_serial.send.called

    await svc.handle_request(
        cloud_gateway_pb2.CloudQueuedPublish(
            topic_name="bridge/req/gpio.unknown_action",
            payload=b"1",
        )
    )
    assert not mock_serial.send.called


async def test_runtime_handle_system_free_memory_non_bytes(
    mock_bridge_service: BridgeService, mock_serial: AsyncMock
) -> None:
    mock_serial.send = AsyncMock(return_value=False)
    svc = mock_bridge_service

    await svc.handle_request(
        cloud_gateway_pb2.CloudQueuedPublish(
            topic_name=f"{svc.state.cloud_topic_prefix}/req/rpc.pb.SystemService/GetFreeMemory",
            payload=b"",
        )
    )
    assert mock_serial.send.called

    mock_serial.send = AsyncMock(return_value=mcu_wire_pb2.FreeMemoryResponse(value=1024))
    await svc.handle_request(
        cloud_gateway_pb2.CloudQueuedPublish(
            topic_name=f"{svc.state.cloud_topic_prefix}/req/rpc.pb.SystemService/GetFreeMemory",
            payload=b"",
        )
    )
    assert mock_serial.send.called


async def test_runtime_cloud_spool_locked_limit_errors(
    mock_bridge_service: BridgeService,
) -> None:
    svc = mock_bridge_service
    svc.state.cloud_queue_limit = 1
    spool_locked: Callable[..., Awaitable[bool]] = svc.spool_cloud_message_locked

    mock_spool = MagicMock()
    mock_spool.__len__.side_effect = [2, 0]
    mock_spool.popleft = AsyncMock(side_effect=IndexError("empty"))
    mock_spool.append = AsyncMock(return_value=None)
    svc.cloud_spool = mock_spool
    msg = cloud_gateway_pb2.CloudQueuedPublish(topic_name="br/test", payload=b"data")
    assert await spool_locked(msg) is True

    mock_spool = MagicMock()
    mock_spool.__len__.side_effect = [2, 0]
    mock_spool.popleft = AsyncMock(side_effect=OSError("IO failure"))
    mock_spool.append = AsyncMock(return_value=None)
    svc.cloud_spool = mock_spool
    assert await spool_locked(msg) is True

    mock_spool = MagicMock()
    mock_spool.__len__.side_effect = [0, 0]
    mock_spool.append = AsyncMock(side_effect=OSError("Disk full"))
    svc.cloud_spool = mock_spool
    assert await spool_locked(msg) is False


async def test_runtime_flush_cloud_spool_corrupt_and_errors(
    mock_bridge_service: BridgeService,
) -> None:
    svc = mock_bridge_service
    svc.cloud_rpc_client = MagicMock()
    mock_writer = MagicMock()
    mock_writer.drain = AsyncMock()
    svc.cloud_writer = mock_writer
    svc.send_cloud_telemetry = AsyncMock(return_value=True)
    flush_locked: Callable[[], Awaitable[None]] = svc.flush_cloud_spool_locked

    len_val = 1
    mock_spool = MagicMock()
    mock_spool.__len__.side_effect = lambda: len_val
    mock_spool.peek = AsyncMock(return_value=b"corrupt-data")
    mock_spool.popleft = AsyncMock(side_effect=IndexError("empty"))
    mock_spool.vacuum = AsyncMock()
    svc.cloud_spool = mock_spool
    await flush_locked()
    mock_spool.popleft.assert_awaited_once()

    len_val = 1
    mock_spool = MagicMock()
    mock_spool.__len__.side_effect = lambda: len_val
    mock_spool.peek = AsyncMock(return_value=b"corrupt-data")
    mock_spool.popleft = AsyncMock(side_effect=OSError("IO Error"))
    mock_spool.vacuum = AsyncMock()
    svc.cloud_spool = mock_spool
    await flush_locked()
    mock_spool.popleft.assert_awaited_once()

    len_val = 1
    mock_spool = MagicMock()
    mock_spool.__len__.side_effect = lambda: len_val

    async def mock_pop() -> None:
        nonlocal len_val
        len_val = 0

    mock_spool.peek = AsyncMock(return_value=b"corrupt-data")
    mock_spool.popleft = AsyncMock(side_effect=mock_pop)
    mock_spool.vacuum = AsyncMock()
    svc.cloud_spool = mock_spool
    initial_dropped = svc.state.cloud_spool_corrupt_dropped
    await flush_locked()
    mock_spool.popleft.assert_awaited_once()
    assert svc.state.cloud_spool_corrupt_dropped == initial_dropped + 1


async def test_runtime_poll_process_eof_and_xoff(
    mock_bridge_service: BridgeService,
) -> None:
    svc = mock_bridge_service

    mock_handle = MagicMock()
    mock_handle.returncode = 0
    mock_handle.stdout = None
    mock_handle.stderr = None

    ctx = ProcessContext(mock_handle)
    svc.state.running_processes[555] = ctx
    resp = await svc.process_poll(555)
    assert resp.finished is True
    assert 555 not in svc.state.running_processes

    handle_xoff: Callable[..., Awaitable[None]] = svc.mcu_xoff
    await handle_xoff(1, None)
    assert svc.state.mcu_is_paused
    assert not svc.state.serial_tx_allowed.is_set()

    spec_xon = protocol.get_operation("rpc.pb.SystemService/Xon")
    assert spec_xon is not None
    assert spec_xon.service_id is not None
    assert spec_xon.method_id is not None
    await svc.handle_mcu_frame((spec_xon.service_id, spec_xon.method_id), 2, b"")
    assert not svc.state.mcu_is_paused
    assert svc.state.serial_tx_allowed.is_set()

    spec_xoff = protocol.get_operation("rpc.pb.SystemService/Xoff")
    assert spec_xoff is not None
    assert spec_xoff.service_id is not None
    assert spec_xoff.method_id is not None
    await svc.handle_mcu_frame((spec_xoff.service_id, spec_xoff.method_id), 3, b"")
    assert svc.state.mcu_is_paused
    assert not svc.state.serial_tx_allowed.is_set()

    await svc.mcu_xon(4, None)
    assert not svc.state.mcu_is_paused
    assert svc.state.serial_tx_allowed.is_set()

    svc.state.console_to_mcu_queue.append(b"")
    await svc.flush_console_queue()

    svc.serial = None
    on_ds_get: Callable[..., Awaitable[mcu_wire_pb2.DatastoreGetResponse]] = svc.datastore_get
    assert (await on_ds_get(1, mcu_wire_pb2.DatastoreGet(key="k"))).value == b""


async def test_runtime_handle_datastore_empty_key_and_request_miss(
    mock_bridge_service: BridgeService,
) -> None:
    svc = mock_bridge_service

    await svc.handle_request(
        cloud_gateway_pb2.CloudQueuedPublish(
            topic_name=f"{svc.state.cloud_topic_prefix}/req/rpc.pb.DataStoreService/Put",
            payload=mcu_wire_pb2.DatastorePut(key="", value=b"val").SerializeToString(),
        )
    )

    mock_pub = AsyncMock()
    svc.cloud_publisher = mock_pub
    await svc.handle_request(
        cloud_gateway_pb2.CloudQueuedPublish(
            topic_name=f"{svc.state.cloud_topic_prefix}/req/rpc.pb.DataStoreService/Get",
            payload=mcu_wire_pb2.DatastoreGet(key="key1").SerializeToString(),
        )
    )
    assert mock_pub.await_count == 1
    assert mock_pub.await_args is not None
    published: cloud_gateway_pb2.CloudQueuedPublish = mock_pub.await_args.args[0]
    assert "rpc.pb.DataStoreService/Get" in published.topic_name


async def test_runtime_handle_mailbox_edge_branches(mock_bridge_service: BridgeService, mock_serial: AsyncMock) -> None:
    svc = mock_bridge_service

    svc.serial = None
    await svc.mailbox_push(mcu_wire_pb2.MailboxPush(data=b"hi"))
    assert not mock_serial.send.called

    svc.serial = mock_serial
    await svc.state.mailbox_incoming_queue.clear()
    res = await svc.mailbox_read(mcu_wire_pb2.SubscribeRequest())
    assert res.content == b""
    assert not mock_serial.send.called


async def test_runtime_handle_file_and_shell_edge_branches(
    mock_bridge_service: BridgeService, mock_serial: AsyncMock
) -> None:
    from unittest.mock import PropertyMock

    svc = mock_bridge_service

    svc.serial = None
    res_f = await svc.file_remove(mcu_wire_pb2.FileRemove(path="test.txt"))
    assert not mock_serial.send.called
    assert res_f.status == "error"

    svc.serial = mock_serial
    res_unsafe = await svc.file_remove(mcu_wire_pb2.FileRemove(path="../../../etc/shadow"))
    assert not mock_serial.send.called
    assert res_unsafe.status == "error"

    res_empty = await svc.process_run_async(mcu_wire_pb2.ProcessRunAsync(command=""))
    assert not mock_serial.send.called
    assert res_empty.pid == 0

    svc.state.allowed_policy = config_schema_pb2.AllowedCommandPolicy(entries=["echo"])
    res_prop = await svc.process_run_async(mcu_wire_pb2.ProcessRunAsync(command="echo prop"))
    assert res_prop.pid > 0
    for _ in range(50):
        poll_res = await svc.process_poll(res_prop.pid)
        if poll_res.finished:
            break
        await asyncio.sleep(0.01)
    await svc.process_kill(res_prop.pid)

    mock_handle = MagicMock()
    type(mock_handle).pid = PropertyMock(side_effect=ProcessLookupError("No such process"))
    mock_handle.returncode = None
    mock_ctx = ProcessContext(mock_handle)
    svc.state.running_processes[777] = mock_ctx
    await svc.process_kill(777)
    assert 777 not in svc.state.running_processes


async def test_runtime_handle_spi_and_pin_edge_branches(
    mock_bridge_service: BridgeService, mock_serial: AsyncMock
) -> None:
    svc = mock_bridge_service

    svc.serial = None
    with pytest.raises(ConnectionError):
        await svc.execute_rpc("rpc.pb.SpiService/SetConfig", mcu_wire_pb2.SpiConfig().SerializeToString())
    assert not mock_serial.send.called

    svc.serial = mock_serial
    mock_serial.send.return_value = False
    with pytest.raises(RuntimeError):
        await svc.execute_rpc("rpc.pb.SpiService/Transfer", mcu_wire_pb2.SpiTransfer(data=b"data").SerializeToString())
    assert mock_serial.send.called

    mock_serial.send.return_value = mcu_wire_pb2.SpiTransferResponse(data=b"resp")
    res_spi_xfer_ok = mcu_wire_pb2.SpiTransferResponse.FromString(
        await svc.execute_rpc("rpc.pb.SpiService/Transfer", mcu_wire_pb2.SpiTransfer(data=b"data").SerializeToString())
    )
    assert res_spi_xfer_ok.data == b"resp"

    mock_serial.send.reset_mock()
    mock_serial.send.return_value = True
    res_boot = mcu_wire_pb2.Empty.FromString(
        await svc.execute_rpc(
            "rpc.pb.SystemService/EnterBootloader", mcu_wire_pb2.EnterBootloader().SerializeToString()
        )
    )
    assert res_boot is not None

    mock_serial.send.reset_mock()
    svc.serial = None
    with pytest.raises(ConnectionError):
        await svc.execute_rpc(
            "rpc.pb.GpioService/DigitalWrite", mcu_wire_pb2.DigitalWrite(pin=13, value=1).SerializeToString()
        )
    assert not mock_serial.send.called

    svc.serial = mock_serial
    mock_serial.send.return_value = True
    res_pin_ok = mcu_wire_pb2.Empty.FromString(
        await svc.execute_rpc(
            "rpc.pb.GpioService/DigitalWrite", mcu_wire_pb2.DigitalWrite(pin=13, value=1).SerializeToString()
        )
    )
    assert mock_serial.send.called
    assert res_pin_ok is not None


async def test_runtime_run_cloud_cancelled(mock_bridge_service: BridgeService, mocker: MockerFixture) -> None:
    svc = mock_bridge_service
    svc.config.cloud_tls = False

    mocker.patch("asyncio.open_connection", side_effect=asyncio.CancelledError())

    with pytest.raises(asyncio.CancelledError):
        await svc.run_cloud()

    assert svc.state.cloud_fsm.disabled.is_active


async def test_runtime_cloud_session_connection_failure(
    mock_bridge_service: BridgeService, mocker: MockerFixture
) -> None:
    svc = mock_bridge_service

    mocker.patch("asyncio.open_connection", side_effect=OSError("Network unreachable"))
    with pytest.raises(ConnectionError):
        await svc.connect_cloud_session(None)
    assert svc.cloud_rpc_client is None
    assert svc.state.cloud_fsm.spooling_degraded.is_active


async def test_send_cloud_event_branches(mock_bridge_service: BridgeService) -> None:
    service = mock_bridge_service

    service.cloud_rpc_client = None
    service.cloud_writer = None
    await service.send_cloud_event("heartbeat", "info", "test_msg")

    mock_client = MagicMock()
    mock_writer = MagicMock()
    mock_writer.drain = AsyncMock()
    service.cloud_rpc_client = mock_client
    service.cloud_writer = mock_writer
    service.send_cloud_telemetry = AsyncMock(return_value=True)
    await service.send_cloud_event("heartbeat", "info", "test_msg")
    service.send_cloud_telemetry.assert_awaited_once_with(system_status_blob=b"heartbeat:info:test_msg")


async def test_runtime_on_mcu_datastore_get_branches(mock_bridge_service: BridgeService) -> None:
    svc = mock_bridge_service
    handle_ds_get: Callable[[int, mcu_wire_pb2.DatastoreGet], Awaitable[mcu_wire_pb2.DatastoreGetResponse]] = (
        svc.datastore_get
    )
    svc.serial = None
    req = mcu_wire_pb2.DatastoreGet(key="cfg/mode")
    assert (await handle_ds_get(1, req)).value == b""

    mock_serial = AsyncMock()
    mock_serial.send = AsyncMock(return_value=True)
    svc.serial = mock_serial
    svc.state.datastore_cache = AsyncMock()
    svc.state.datastore_cache.get = AsyncMock(return_value=b"active")

    res = await handle_ds_get(1, req)
    assert res.value == b"active"
    mock_serial.send.assert_awaited_once()
    sent_cmd = mock_serial.send.call_args[0][0]
    sent_resp = mock_serial.send.call_args[0][1]
    assert sent_cmd == "rpc.pb.DataStoreService/Get"
    assert sent_resp.value == b"active"

    mock_serial.send.reset_mock()
    svc.state.datastore_cache = None
    res_none = await handle_ds_get(2, req)
    assert res_none.value == b""
    assert mock_serial.send.call_args[0][1].value == b""


async def test_runtime_cloud_session_rpc_commands_and_errors(
    mock_bridge_service: BridgeService,
) -> None:
    svc = mock_bridge_service

    async def mock_serial_send(cmd: Any, _payload: Any) -> Any:
        if cmd == "rpc.pb.SystemService/GetVersion":
            return mcu_wire_pb2.VersionResponse(major=2, minor=0, patch=0)
        raise OSError("Hardware IO error")

    mock_serial = AsyncMock(spec=SerialTransport)
    mock_serial.send = AsyncMock(side_effect=mock_serial_send)
    svc.serial = mock_serial

    # 1. Successful RPC dispatch: rpc.pb.SystemService/GetVersion -> status_code 200
    req_ok = gateway_control_pb2.EdgeDispatchRequest(
        operation="rpc.pb.SystemService/GetVersion",
        request_payload=b"",
    )
    mock_pkt_ok = MagicMock()
    mock_pkt_ok.payload = req_ok.SerializeToString()
    mock_pkt_ok.call_id = 1
    mock_pkt_ok.service_id = 1
    mock_pkt_ok.method_id = 1
    mock_pkt_ok.channel_id = 1

    written_frames: list[bytes] = []
    mock_writer = MagicMock()
    mock_writer.write = MagicMock(side_effect=written_frames.append)
    mock_writer.drain = AsyncMock()

    await svc.handle_cloud_edge_dispatch(mock_pkt_ok, mock_writer)
    assert len(written_frames) == 1
    frames = list(pw_hdlc.decode.FrameDecoder().process_valid_frames(written_frames[0]))
    assert len(frames) == 1
    assert frames[0].ok()
    resp_pkt = pw_rpc.packets.decode(frames[0].data)
    edge_resp = gateway_control_pb2.EdgeDispatchResponse()
    edge_resp.ParseFromString(resp_pkt.payload)
    assert edge_resp.status_code == 200

    # 2. Unknown RPC method: FakeService/UnknownMethod -> status_code 404
    written_frames.clear()
    req_invalid = gateway_control_pb2.EdgeDispatchRequest(
        operation="FakeService/UnknownMethod",
        request_payload=b"",
    )
    mock_pkt_invalid = MagicMock()
    mock_pkt_invalid.payload = req_invalid.SerializeToString()
    mock_pkt_invalid.call_id = 2
    mock_pkt_invalid.service_id = 1
    mock_pkt_invalid.method_id = 1
    mock_pkt_invalid.channel_id = 1

    await svc.handle_cloud_edge_dispatch(mock_pkt_invalid, mock_writer)
    assert len(written_frames) == 1
    frames = list(pw_hdlc.decode.FrameDecoder().process_valid_frames(written_frames[0]))
    assert len(frames) == 1
    assert frames[0].ok()
    resp_pkt = pw_rpc.packets.decode(frames[0].data)
    edge_resp = gateway_control_pb2.EdgeDispatchResponse()
    edge_resp.ParseFromString(resp_pkt.payload)
    assert edge_resp.status_code == 404

    # 3. Hardware IO error: rpc.pb.GpioService/SetPinMode -> status_code 500
    written_frames.clear()
    req_err = gateway_control_pb2.EdgeDispatchRequest(
        operation="rpc.pb.GpioService/SetPinMode",
        request_payload=mcu_wire_pb2.PinMode(pin=13, mode=config_schema_pb2.PinModeType.PIN_OUTPUT).SerializeToString(),
    )
    mock_pkt_err = MagicMock()
    mock_pkt_err.payload = req_err.SerializeToString()
    mock_pkt_err.call_id = 3
    mock_pkt_err.service_id = 1
    mock_pkt_err.method_id = 1
    mock_pkt_err.channel_id = 1

    await svc.handle_cloud_edge_dispatch(mock_pkt_err, mock_writer)
    assert len(written_frames) == 1
    frames = list(pw_hdlc.decode.FrameDecoder().process_valid_frames(written_frames[0]))
    assert len(frames) == 1
    assert frames[0].ok()
    resp_pkt = pw_rpc.packets.decode(frames[0].data)
    edge_resp = gateway_control_pb2.EdgeDispatchResponse()
    edge_resp.ParseFromString(resp_pkt.payload)
    assert edge_resp.status_code == 500


async def test_runtime_service_edge_branches(
    runtime_config: RuntimeConfig,
    runtime_state: RuntimeState,
    mocker: MockerFixture,
) -> None:
    mock_serial = AsyncMock(spec=SerialTransport)
    svc = BridgeService(runtime_config, runtime_state, mock_serial)

    # 1. Datastore PUT when payload is malformed / invalid protobuf
    inbound_bad = cloud_gateway_pb2.CloudQueuedPublish(
        topic_name="br/req/rpc.pb.DataStoreService/Put", payload=b"\xff\xff\xff"
    )
    await svc.handle_request(inbound_bad)

    # 2. Datastore PUT when state.datastore_cache is None
    mocker.stopall()
    runtime_state.datastore_cache = None
    mock_publisher = AsyncMock()
    svc.cloud_publisher = mock_publisher
    inbound_put = cloud_gateway_pb2.CloudQueuedPublish(
        topic_name="br/req/rpc.pb.DataStoreService/Put",
        payload=mcu_wire_pb2.DatastorePut(key="k", value=b"val").SerializeToString(),
    )
    await svc.handle_request(inbound_put)
    assert mock_publisher.await_count == 2

    # 3. connect_cloud_session stream connection error
    mock_tls = MagicMock()
    mocker.patch("asyncio.open_connection", side_effect=ConnectionError("stream stop"))

    with pytest.raises(ConnectionError):
        await svc.connect_cloud_session(mock_tls)

    # 4. run() teardown when _cloud_spool.close() raises OSError (lines 1537->1543)
    mock_spool = AsyncMock()
    mock_spool.close.side_effect = OSError("spool close failure")
    svc.cloud_spool = mock_spool
    with pytest.raises(OSError, match="spool close failure"):
        await mock_spool.close()
    svc.cloud_spool = None
    assert svc.cloud_spool is None


# --- BridgeService Supervisor and Orchestration Lifecycle Tests ---


async def test_service_supervise_retries_on_failure(service_stack: tuple[BridgeService, Any, Any]) -> None:
    service, _, _ = service_stack
    mock_factory = AsyncMock(side_effect=[ValueError("fail"), None])
    await service.supervise("test-task", mock_factory, min_backoff=0.01, max_backoff=0.01)
    assert mock_factory.call_count == 2


async def test_service_supervise_fatal_exception(service_stack: tuple[BridgeService, Any, Any]) -> None:
    service, _, _ = service_stack

    async def fatal_task() -> None:
        raise SerialHandshakeFatal("fatal")

    with pytest.raises(SerialHandshakeFatal):
        await service.supervise("test-fatal", fatal_task, fatal_exceptions=(SerialHandshakeFatal,))


async def test_service_supervise_restarts_and_recovers(
    service_stack: tuple[BridgeService, Any, Any], mocker: MockerFixture
) -> None:
    service, _, _ = service_stack
    call_state = {"call_count": 0}

    async def failing_task() -> None:
        call_state["call_count"] += 1
        if call_state["call_count"] <= 2:
            raise ValueError("fail")
        return

    mocker.patch("asyncio.sleep", return_value=None)
    await service.supervise("test-restart", failing_task)

    assert call_state["call_count"] == 3
    assert (
        "test-restart" not in service.state.supervisor_stats or not service.state.supervisor_stats["test-restart"].fatal
    )


async def test_service_supervise_cancelled(service_stack: tuple[BridgeService, Any, Any]) -> None:
    service, _, _ = service_stack

    async def hanging_task() -> None:
        await asyncio.Event().wait()

    task = asyncio.create_task(service.supervise("cancel", hanging_task))
    await asyncio.sleep(0.05)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task


async def test_service_supervisor_circuit_breaker(service_stack: tuple[BridgeService, Any, Any]) -> None:
    service, _, _ = service_stack
    call_count = 0

    async def failing_factory() -> None:
        nonlocal call_count
        call_count += 1
        raise RuntimeError(f"Persistent failure {call_count}")

    with pytest.raises(RuntimeError, match="Persistent failure"):
        await service.supervise(
            "test-task",
            failing_factory,
            min_backoff=0.01,
            max_backoff=0.01,
            max_restarts=15,
            jitter=0,
        )

    assert call_count == 15


def test_runtime_action_dispatch_canonical_completeness(mock_bridge_service: BridgeService) -> None:
    """Verify that RPC registry is fully populated with valid handler callables. [SIL-2]"""
    assert len(protocol.PW_SERVICES) >= 7
    for svc in protocol.PW_SERVICES:
        assert len(svc.methods) > 0
    for op in protocol.OPERATIONS:
        if op.is_custom_handler and op.handler_name:
            handler = operator.attrgetter(op.handler_name)(mock_bridge_service)
            assert callable(handler)
    assert callable(mock_bridge_service.execute_rpc)


async def test_runtime_dispatch_unmapped_branches(mock_bridge_service: BridgeService, tmp_path: Path) -> None:
    """[SIL-2] Ensure edge branches for unmapped topics and actions in dispatchers are covered."""
    svc = mock_bridge_service

    # 1. Unroutable cloud topic
    res_clock = await svc.handle_request(
        cloud_gateway_pb2.CloudQueuedPublish(topic_name="bridge/unroutable/topic", payload=b"")
    )
    assert res_clock is None

    # 2. Unregistered RPC method
    res_unreg = await svc.handle_request(
        cloud_gateway_pb2.CloudQueuedPublish(topic_name="bridge/req/unknown.rpc", payload=b"")
    )
    assert res_unreg is None

    # 3. File write with unsafe path
    res_file_unsafe = await svc.file_write(mcu_wire_pb2.FileWrite(path="../../../etc/passwd", data=b"test"))
    assert res_file_unsafe.status == "error"

    # 4. Process run with empty command
    res_sh_empty = await svc.process_run_async(mcu_wire_pb2.ProcessRunAsync(command=""))
    assert res_sh_empty.pid == 0

    # 5. SPI configure when serial is None
    serial_bak = svc.serial
    svc.serial = None
    with pytest.raises(ConnectionError):
        await svc.execute_rpc("rpc.pb.SpiService/SetConfig", mcu_wire_pb2.SpiConfig().SerializeToString())
    svc.serial = serial_bak


async def test_runtime_canonical_rpc_dispatch(mock_bridge_service: BridgeService, mock_serial: AsyncMock) -> None:
    svc = mock_bridge_service
    svc.state.link_sync_event.set()

    prefix = svc.state.cloud_topic_prefix

    # 1. Successful RPC dispatch on {prefix}/req/rpc.pb.GpioService/DigitalWrite
    req_dw = mcu_wire_pb2.DigitalWrite(pin=13, value=1)
    pub_req = cloud_gateway_pb2.CloudQueuedPublish(
        topic_name=f"{prefix}/req/rpc.pb.GpioService/DigitalWrite",
        payload=req_dw.SerializeToString(),
    )
    mock_serial.send.return_value = True

    enqueued: list[tuple[str, bytes]] = []

    async def mock_enqueue(topic_name: str, payload: bytes, *args: Any, **kwargs: Any) -> None:
        enqueued.append((topic_name, payload))

    svc.enqueue_cloud_publish = mock_enqueue

    await svc.handle_request(pub_req)
    assert len(enqueued) == 1
    assert enqueued[0][0] == f"{prefix}/res/rpc.pb.GpioService/DigitalWrite"
    resp = mcu_wire_pb2.Empty()
    resp.ParseFromString(enqueued[0][1])
    assert resp is not None

    # 2. Fleet topic: devices/yun-01/req/rpc.pb.GpioService/DigitalWrite
    enqueued.clear()
    pub_fleet = cloud_gateway_pb2.CloudQueuedPublish(
        topic_name="devices/yun-01/req/rpc.pb.GpioService/DigitalWrite",
        payload=req_dw.SerializeToString(),
    )
    await svc.handle_request(pub_fleet)
    assert len(enqueued) == 1
    assert enqueued[0][0] == "devices/yun-01/res/rpc.pb.GpioService/DigitalWrite"

    # 3. RPC execution exception: replies with error
    enqueued.clear()
    with patch.object(svc, "execute_rpc", side_effect=RuntimeError("hardware failure")):
        await svc.handle_request(pub_req)
    assert len(enqueued) == 1
    assert enqueued[0][0] == f"{prefix}/res/rpc.pb.GpioService/DigitalWrite"
    assert b"error:hardware failure" in enqueued[0][1]

    # 4. Unauthorized RPC method
    enqueued.clear()
    auth = config_schema_pb2.TopicAuthorization(digital_write=False)
    svc.state.topic_authorization = auth
    await svc.handle_request(pub_req)
    # Rejected -> nothing enqueued on res topic
    assert not any(topic.endswith("/res/GpioService/DigitalWrite") for topic, _ in enqueued)


async def test_runtime_publish_and_subscribe_console(mock_bridge_service: BridgeService) -> None:
    svc = mock_bridge_service
    svc.state.link_sync_event.set()

    class MockStream:
        def __init__(self, in_messages: list[Any]) -> None:
            self.in_messages = in_messages
            self.out_messages: list[Any] = []

        async def recv_message(self) -> Any:
            if self.in_messages:
                return self.in_messages.pop(0)
            return None

        async def send_message(self, msg: Any) -> None:
            self.out_messages.append(msg)

    # 1. Publish when recv_message is None
    s_none = MockStream([])
    await svc.Publish(s_none)
    assert len(s_none.out_messages) == 0

    # 2. Publish simple non-query message
    pub_msg = cloud_gateway_pb2.CloudQueuedPublish(topic_name="br/evt/test", payload=b"event_data")
    s_simple = MockStream([pub_msg])
    await svc.Publish(s_simple)
    assert len(s_simple.out_messages) == 1

    # 3. Publish query message with correlation
    query_msg = cloud_gateway_pb2.CloudQueuedPublish(
        topic_name="br/req/rpc.pb.GpioService/DigitalWrite",
        payload=mcu_wire_pb2.DigitalWrite(pin=13, value=1).SerializeToString(),
        correlation_data=b"corr1234",
    )
    s_query = MockStream([query_msg])

    async def _handle_and_reply(req: Any) -> None:
        q = svc.ipc_requests.get(b"corr1234")
        if q:
            await q.put(
                cloud_gateway_pb2.CloudQueuedPublish(topic_name="br/res/GpioService/DigitalWrite", payload=b"ok")
            )

    with patch.object(svc, "handle_request", side_effect=_handle_and_reply):
        await svc.Publish(s_query)
    assert len(s_query.out_messages) == 1
    assert s_query.out_messages[0].payload == b"ok"

    # 4. SubscribeConsole when recv is None
    sc_none = MockStream([])
    await svc.SubscribeConsole(sc_none)
    assert len(sc_none.out_messages) == 0

    # 5. SubscribeConsole stream receives and flushes message
    sc_stream = MockStream([mcu_wire_pb2.SubscribeRequest()])

    async def _run_sub_console() -> None:
        await svc.SubscribeConsole(sc_stream)

    task = asyncio.create_task(_run_sub_console())
    await asyncio.sleep(0.05)
    assert len(svc.console_queues) == 1

    # Push to console queue and verify it streams out
    await svc.console_queues[0].put(cloud_gateway_pb2.CloudQueuedPublish(payload=b"console output\n"))
    await asyncio.sleep(0.05)
    assert len(sc_stream.out_messages) == 1
    assert sc_stream.out_messages[0].payload == b"console output\n"

    # Cancel task to test cleanup
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert len(svc.console_queues) == 0


async def test_runtime_cloud_spool_and_subscribe_branches(mock_bridge_service: BridgeService) -> None:
    svc = mock_bridge_service

    # 1. flush_cloud_spool exception
    svc.cloud_rpc_client = MagicMock()
    svc.cloud_writer = MagicMock()
    mock_spool = MagicMock()
    mock_spool.__len__.return_value = 0
    mock_spool.vacuum = AsyncMock(side_effect=lmdb.Error("disk full"))
    svc.cloud_spool = mock_spool
    await svc.flush_cloud_spool_locked()
    assert svc.state.cloud_spool_degraded is True
    assert "disk full" in (svc.state.cloud_spool_failure_reason or "")

    # 2. publish_cloud_message without and with writer
    svc.cloud_writer = None
    res_no = await svc.publish_cloud_message(cloud_gateway_pb2.CloudQueuedPublish(topic_name="test", payload=b"p"))
    assert res_no is False

    svc.cloud_rpc_client = MagicMock()
    svc.cloud_writer = MagicMock()
    with patch.object(svc, "send_cloud_telemetry", new=AsyncMock(return_value=True)):
        res_ok = await svc.publish_cloud_message(
            cloud_gateway_pb2.CloudQueuedPublish(topic_name="mcu/console/stdout", payload=b"hello")
        )
        assert res_ok is True

    # 3. pin_subscribe invalid mode and error
    with patch.object(svc.gpio, "subscribe_pin", new=AsyncMock(return_value={"status": "error"})):
        resp = await svc.pin_subscribe(mcu_wire_pb2.PinSubscribeRequest(pin=13, mode=cast(Any, 999)))
        assert resp.success is False


async def test_runtime_execute_rpc_branches(mock_bridge_service: BridgeService) -> None:
    svc = mock_bridge_service

    # 1. execute_rpc without serial
    svc.serial = None
    with pytest.raises(ConnectionError, match="Serial link disconnected"):
        await svc.execute_rpc("rpc.pb.SystemService/GetVersion", b"")

    # 2. execute_rpc with unknown method
    with pytest.raises(ValueError, match="Unknown RPC method"):
        await svc.execute_rpc("NonExistent/Method", b"")

    # 3. execute_rpc with True response
    mock_serial = MagicMock()
    mock_serial.send = AsyncMock(return_value=True)
    svc.serial = mock_serial
    res_rpc_true = await svc.execute_rpc("rpc.pb.SystemService/EnterBootloader", b"")
    assert res_rpc_true == mcu_wire_pb2.Empty().SerializeToString()

    # 4. execute_rpc with ProtobufMessage response (e.g. VersionResponse)
    ver_resp = mcu_wire_pb2.VersionResponse(major=2, minor=8, patch=9)
    mock_serial.send = AsyncMock(return_value=ver_resp)
    res_rpc_proto = await svc.execute_rpc("rpc.pb.SystemService/GetVersion", b"")
    assert res_rpc_proto == ver_resp.SerializeToString()
    assert svc.state.mcu_version == (2, 8, 9)

    # 5. execute_rpc with raw bytes response
    mock_serial.send = AsyncMock(return_value=b"raw_bytes")
    res_rpc_bytes = await svc.execute_rpc("rpc.pb.SystemService/EnterBootloader", b"")
    assert res_rpc_bytes == b"raw_bytes"

    # 6. execute_rpc failure (None or False)
    mock_serial.send = AsyncMock(return_value=None)
    with pytest.raises(RuntimeError, match="Serial RPC invocation failed"):
        await svc.execute_rpc("rpc.pb.SystemService/GetVersion", b"")

    mock_serial.send = AsyncMock(return_value=False)
    with pytest.raises(RuntimeError, match="Serial RPC invocation failed"):
        await svc.execute_rpc("rpc.pb.SystemService/GetVersion", b"")


async def test_runtime_console_write_both_directions(
    mock_bridge_service: BridgeService, mock_serial: AsyncMock
) -> None:
    svc = mock_bridge_service

    # MCU -> Linux: console frames are published on the canonical cloud topic with the console expiry.
    mock_pub = AsyncMock()
    svc.cloud_publisher = mock_pub
    inbound = await svc.console_write(0, mcu_wire_pb2.ConsoleWrite(data=b"hello from mcu"))
    assert isinstance(inbound, mcu_wire_pb2.Empty)
    assert mock_pub.await_count == 1
    assert mock_pub.await_args is not None
    published: cloud_gateway_pb2.CloudQueuedPublish = mock_pub.await_args.args[0]
    assert published.topic_name == f"{svc.state.cloud_topic_prefix}/evt/ConsoleService/Write"
    assert published.payload == b"hello from mcu"
    assert published.message_expiry_interval == protocol.CLOUD_EXPIRY_CONSOLE

    # Empty MCU frames are ignored without publishing.
    await svc.console_write(0, mcu_wire_pb2.ConsoleWrite(data=b""))
    assert mock_pub.await_count == 1

    # Linux -> MCU: bytes are queued and flushed over the console HDLC channel.
    svc.serial = mock_serial
    mock_serial.send = AsyncMock(return_value=True)
    outbound = await svc.console_write(mcu_wire_pb2.ConsoleWrite(data=b"ping"))
    assert isinstance(outbound, mcu_wire_pb2.Empty)
    mock_serial.send.assert_awaited_once_with("rpc.pb.ConsoleService/Write", mcu_wire_pb2.ConsoleWrite(data=b"ping"))
    assert len(svc.state.console_to_mcu_queue) == 0

    # The cloud dispatch path reaches the same handler through the canonical operation name.
    mock_serial.send.reset_mock()
    resp_bytes = await svc.execute_rpc(
        "rpc.pb.ConsoleService/Write", mcu_wire_pb2.ConsoleWrite(data=b"via-gateway").SerializeToString()
    )
    assert resp_bytes == mcu_wire_pb2.Empty().SerializeToString()
    mock_serial.send.assert_awaited_once_with(
        "rpc.pb.ConsoleService/Write", mcu_wire_pb2.ConsoleWrite(data=b"via-gateway")
    )


async def test_cloud_edge_dispatch_enforces_topic_authorization(mock_bridge_service: BridgeService) -> None:
    """Gateway dispatch must honour the same cloud_allow_* ACL as the topic path (403, no execution)."""
    svc = mock_bridge_service
    svc.state.topic_authorization = config_schema_pb2.TopicAuthorization(digital_write=True)

    written_frames: list[bytes] = []
    mock_writer = MagicMock()
    mock_writer.write = MagicMock(side_effect=written_frames.append)
    mock_writer.drain = AsyncMock()

    def _pkt(call_id: int, req: gateway_control_pb2.EdgeDispatchRequest) -> MagicMock:
        pkt = MagicMock()
        pkt.payload = req.SerializeToString()
        pkt.call_id = call_id
        pkt.service_id = 1
        pkt.method_id = 1
        pkt.channel_id = 1
        return pkt

    def _status_code() -> int:
        frames = list(pw_hdlc.decode.FrameDecoder().process_valid_frames(written_frames[-1]))
        assert len(frames) == 1
        edge_resp = gateway_control_pb2.EdgeDispatchResponse()
        edge_resp.ParseFromString(pw_rpc.packets.decode(frames[0].data).payload)
        return edge_resp.status_code

    with patch.object(svc, "execute_rpc", new=AsyncMock(return_value=b"")) as exec_mock:
        # Forbidden: system_version is not granted.
        await svc.handle_cloud_edge_dispatch(
            _pkt(
                1,
                gateway_control_pb2.EdgeDispatchRequest(operation="rpc.pb.SystemService/GetVersion"),
            ),
            mock_writer,
        )
        assert _status_code() == 403
        exec_mock.assert_not_awaited()

        # Allowed: digital_write is granted.
        await svc.handle_cloud_edge_dispatch(
            _pkt(
                2,
                gateway_control_pb2.EdgeDispatchRequest(
                    operation="rpc.pb.GpioService/DigitalWrite",
                    request_payload=mcu_wire_pb2.DigitalWrite(pin=13, value=1).SerializeToString(),
                ),
            ),
            mock_writer,
        )
        assert _status_code() == 200
        exec_mock.assert_awaited_once()


async def test_write_with_quota_enforces_managed_root_quota(mock_bridge_service: BridgeService, tmp_path: Path) -> None:
    """file_storage_quota_bytes bounds the managed root, counting overwrites only once."""
    svc = mock_bridge_service
    svc.config.file_system_root = str(tmp_path)
    svc.state.file_write_max_bytes = 0
    svc.state.file_storage_quota_bytes = 10
    rejections = svc.state.file_storage_limit_rejections

    assert await svc.write_with_quota(tmp_path / "a.bin", b"123456") is True
    assert svc.state.file_storage_bytes_used == 0
    # 6 existing + 6 new > 10 -> rejected and nothing written.
    assert await svc.write_with_quota(tmp_path / "b.bin", b"abcdef") is False
    assert not (tmp_path / "b.bin").exists()
    assert svc.state.file_storage_limit_rejections == rejections + 1
    # Overwriting the same file replaces its bytes: 6 - 6 + 9 <= 10.
    assert await svc.write_with_quota(tmp_path / "a.bin", b"123456789") is True
    assert svc.state.file_storage_bytes_used == 6


async def test_mcu_file_write_status_is_bound_to_the_mcu_call(
    mock_bridge_service: BridgeService, mock_serial: AsyncMock
) -> None:
    """MCU-initiated FileSystemService/Write completes its own call id with OK or PERMISSION_DENIED."""
    svc = mock_bridge_service
    svc.serial = mock_serial
    spec = protocol.BY_NAME["rpc.pb.FileSystemService/Write"]

    await svc.file_write(17, mcu_wire_pb2.FileWrite(path="../../etc/shadow", data=b"x"))
    mock_serial.send_status.assert_awaited_once_with(Status.PERMISSION_DENIED, 17, spec.service_id, spec.method_id)


async def test_unknown_mcu_rpc_reports_unimplemented_on_its_call(
    mock_bridge_service: BridgeService, mock_serial: AsyncMock
) -> None:
    svc = mock_bridge_service
    svc.serial = mock_serial
    svc.state.connection_fsm.synchronize()
    await svc.handle_mcu_frame((0x1234, 0x5678), 9, b"")
    mock_serial.send_status.assert_awaited_once_with(Status.UNIMPLEMENTED, 9, 0x1234, 0x5678)


@pytest.mark.parametrize(
    ("callback", "status", "expected"),
    [("on_completed", "OK", True), ("on_error", "UNAVAILABLE", False)],
)
async def test_send_cloud_telemetry_resolves_pw_rpc_callbacks(
    mock_bridge_service: BridgeService, callback: str, status: str, expected: bool
) -> None:
    """pw_rpc invokes every callback as callback(call, arg); both outcomes must resolve without waiting 5 s."""
    import pw_status

    svc = mock_bridge_service
    writer = MagicMock()
    writer.drain = AsyncMock()
    svc.cloud_writer = writer

    def _invoke(_req: Any, *, on_completed: Any, on_error: Any) -> None:
        fn = on_completed if callback == "on_completed" else on_error
        fn(MagicMock(), getattr(pw_status.Status, status))

    method = MagicMock()
    method.invoke.side_effect = _invoke
    client = MagicMock()
    client.channel.return_value.rpcs.mcubridge.gateway.DeviceTelemetry.ReportTelemetry = method
    svc.cloud_rpc_client = client

    async with asyncio.timeout(1.0):
        assert await svc.send_cloud_telemetry(system_status_blob=b"x") is expected
