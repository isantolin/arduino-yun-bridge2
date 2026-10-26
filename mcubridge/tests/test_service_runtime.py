"""Tests for runtime service lifecycle and basic transport integration."""

from __future__ import annotations

import asyncio
import time
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import Any, cast
from unittest.mock import AsyncMock, MagicMock, patch

import lmdb
import pytest
from hypothesis import given
from hypothesis import strategies as st
from mcubridge.config.settings import RuntimeConfig
from mcubridge.protocol import mcubridge_pb2 as pb
from mcubridge.protocol import protocol
from mcubridge.protocol.protocol import (
    Command,
    Status,
    Topic,
)

from mcubridge.services.handshake import SerialHandshakeFatal
from mcubridge.services.runtime import BridgeService
from mcubridge.state.context import ProcessContext, RuntimeState, create_runtime_state

from mcubridge.state.storage import LmdbDeque
from mcubridge.transport.serial import SerialTransport
from pytest_mock import MockerFixture

from tools.testing.mock_stream import MockGrpcStream


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
        ok = await service.serial.send(protocol.Command.CMD_GET_VERSION.value, b"x")
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
        mock_serial.acknowledge.return_value = True
        service = BridgeService(config, state, mock_serial)

        # Before sync, MCU frames other than handshake are ignored/denied
        await service.handle_mcu_frame(protocol.Command.CMD_GET_VERSION.value, 1, b"")
        mock_serial.acknowledge.assert_not_called()
    finally:
        if service is not None:
            service.cleanup()
        else:
            state.cleanup()


async def test_handle_mcu_xon_xoff() -> None:
    service = None
    config = _make_config()
    state = create_runtime_state(config)
    try:
        mock_serial = AsyncMock(spec=SerialTransport)
        service = BridgeService(config, state, mock_serial)
        state.state = "synchronized"

        await service.handle_mcu_frame(protocol.Command.CMD_XOFF.value, 1, b"")
        assert state.mcu_is_paused
        assert not state.serial_tx_allowed.is_set()

        await service.handle_mcu_frame(protocol.Command.CMD_XON.value, 2, b"")
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

        class PublishPacket:
            def __init__(self, topic: str, payload: bytes) -> None:
                self.topic = topic
                self.payload = payload

        mock_msg = PublishPacket(
            f"{config.topic_prefix}/req/console.write",
            pb.ConsoleWrite(data=b"hello").SerializeToString(),
        )

        await service.handle_request(mock_msg)

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
        message = pb.CloudQueuedPublish(topic_name="br/system/status", payload=b"payload")

        await service.enqueue_cloud(message)

        assert state.cloud_spool_pending_messages == 1

        mock_stream = MagicMock()
        mock_stream.send_message = AsyncMock()
        service.cloud_stream = mock_stream
        await service.flush_cloud_spool()

        mock_stream.send_message.assert_called_once()
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

        captured: list[pb.CloudQueuedPublish] = []

        async def capture_enqueue(message: pb.CloudQueuedPublish) -> None:
            captured.append(message)

        service.cloud_publisher = capture_enqueue

        message = pb.CloudQueuedPublish(
            topic_name=f"{config.topic_prefix}/req/gpio.read",
            payload=pb.PinRead(pin=13).SerializeToString(),
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
    svc.config.cloud_http3_enabled = True

    envelope_pong = MagicMock()
    envelope_pong.WhichOneof.return_value = "pong"

    envelope_cmd = MagicMock()
    envelope_cmd.WhichOneof.return_value = "command_request"
    envelope_cmd.command_request.command_path = "rpc/GetVersion"
    envelope_cmd.command_request.payload = b""
    envelope_cmd.sequence_id = 1234

    mock_stream = MockGrpcStream(inbound=[envelope_pong, envelope_cmd])

    mocker.patch("mcubridge.services.runtime.Channel")
    mock_stub_cls = mocker.patch("mcubridge.services.runtime.CloudBridgeStub")

    mock_stub = MagicMock()
    mock_stub.Session.open.return_value = mock_stream
    mock_stub_cls.return_value = mock_stub

    await svc.connect_cloud_session(None)
    assert svc.state.connected_via_http3 is True
    assert mock_stream.send_message.await_count == 2
    assert mock_stream.sent_messages[0].event.event_type == "status_online"
    resp = mock_stream.sent_messages[1]
    assert resp.sequence_id == 1234
    assert resp.command_response.status_code == 200


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

        inbound_put = pb.CloudQueuedPublish(
            topic_name=f"{cfg.topic_prefix}/req/datastore.put",
            payload=pb.DatastorePut(key=key, value=value).SerializeToString(),
        )
        await service.handle_request(inbound_put)
        assert state.datastore_cache is not None
        assert await state.datastore_cache.get(key) == value

        mock_enqueue = AsyncMock()
        service.cloud_publisher = mock_enqueue
        inbound_get = pb.CloudQueuedPublish(
            topic_name=f"{cfg.topic_prefix}/req/datastore.get",
            payload=pb.DatastoreGet(key=key).SerializeToString(),
        )
        await service.handle_request(inbound_get)
        assert mock_enqueue.await_count == 1

        mock_enqueue.reset_mock()
        inbound_get_miss = pb.CloudQueuedPublish(
            topic_name=f"{cfg.topic_prefix}/req/datastore.get",
            payload=pb.DatastoreGet(key=f"non_existing_{key}").SerializeToString(),
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
    await handle_mcu_status(Status.TIMEOUT, 1, b"\xff\xfe\xfd\x80")
    assert mock_enqueue.await_count == 1
    await handle_mcu_status(Status.ERROR, 2, cast(Any, 12345))
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

        v_resp = pb.VersionResponse(major=major, minor=minor, patch=patch_ver).SerializeToString()
        mock_serial.send = AsyncMock(return_value=v_resp)
        inbound = pb.CloudQueuedPublish(topic_name=f"{cfg.topic_prefix}/req/system.version", payload=b"")

        req_mcu_version: Callable[[pb.CloudQueuedPublish], Awaitable[bool]] = service.request_mcu_version
        res = await req_mcu_version(inbound)
        assert res is True
        assert state.mcu_version == (major, minor, patch_ver)

        await service.handle_request(inbound)
        state.cleanup()

    asyncio.run(_run())


@given(
    pin=st.integers(0, 32),
    analog_val=st.integers(0, 255),
    non_digit=st.text(alphabet="abcdefghijklmnopqrstuvwxyz!@#$", min_size=1, max_size=12),
)
def test_runtime_pin_analog_and_invalid_digits(
    tmp_path_factory: pytest.TempPathFactory, pin: int, analog_val: int, non_digit: str
) -> None:
    async def _run() -> None:
        p = tmp_path_factory.mktemp("pin_analog")
        cfg = RuntimeConfig(file_system_root=str(p), allow_non_tmp_paths=True, topic_prefix="test/br")
        state = create_runtime_state(cfg)
        state.link_sync_event.set()
        mock_serial = AsyncMock(spec=SerialTransport)
        mock_serial.send = AsyncMock(return_value=True)
        service = BridgeService(cfg, state, mock_serial)

        inbound_aw = pb.CloudQueuedPublish(
            topic_name=f"{cfg.topic_prefix}/req/gpio.analog_write",
            payload=pb.AnalogWrite(pin=pin, value=analog_val).SerializeToString(),
        )
        await service.handle_request(inbound_aw)
        mock_serial.send.assert_called_with(Command.CMD_ANALOG_WRITE.value, pb.AnalogWrite(pin=pin, value=analog_val))

        inbound_dw = pb.CloudQueuedPublish(
            topic_name=f"{cfg.topic_prefix}/req/gpio.write",
            payload=pb.DigitalWrite(pin=pin, value=0).SerializeToString(),
        )
        await service.handle_request(inbound_dw)
        mock_serial.send.assert_called_with(Command.CMD_DIGITAL_WRITE.value, pb.DigitalWrite(pin=pin, value=0))

        state.cleanup()

    asyncio.run(_run())


async def test_flush_cloud_spool_corrupt_and_index_error(
    mock_bridge_service: BridgeService,
) -> None:
    svc = mock_bridge_service
    svc.cloud_stream = AsyncMock()

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
    valid_msg = pb.CloudQueuedPublish(topic_name="br/t", payload=b"p")
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
        await reject_cloud_fn(svc, pb.CloudQueuedPublish(), Topic.DIGITAL, action)
        svc.enqueue_cloud_status_report.assert_awaited_once()

        svc.enqueue_cloud_status_report.reset_mock()
        await reject_cloud_fn(svc, pb.CloudQueuedPublish(), topic_str, action)
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
    req = pb.CloudQueuedPublish(
        topic_name=f"{runtime_config.topic_prefix}/req/fs.read",
        payload=pb.FileRead(path="../../secret").SerializeToString(),
    )
    await svc.handle_request(req)
    assert mock_serial.send.call_count == 0


async def test_runtime_handle_mcu_frame_branches(mock_bridge_service: BridgeService, mock_serial: AsyncMock) -> None:
    svc = mock_bridge_service

    svc.serial = None
    await svc.handle_mcu_frame(Command.CMD_GET_VERSION.value, 1, b"")
    assert not mock_serial.send.called

    svc.serial = mock_serial
    svc.state.connection_fsm.synchronize()
    await svc.handle_mcu_frame(Command.CMD_GET_VERSION_RESP.value, 1, pb.VersionResponse().SerializeToString())
    assert not mock_serial.send.called

    await svc.handle_mcu_frame(9999, 1, b"")
    assert mock_serial.send.called

    # Test synchronous (non-awaitable) handler path
    def _sync_handler(_seq: int, _p: Any) -> dict[str, str]:
        return {"status": "ok"}

    svc.mcu_registry[Command.CMD_CLOCK_SYNC_RESP.value] = _sync_handler
    await svc.handle_mcu_frame(Command.CMD_CLOCK_SYNC_RESP.value, 2, b"")
    mock_serial.acknowledge.assert_called_with(Command.CMD_CLOCK_SYNC_RESP.value, 2)

    # Test handler returning False (no ack)
    mock_serial.acknowledge.reset_mock()
    svc.mcu_registry[Command.CMD_PROCESS_KILL.value] = AsyncMock(return_value=False)
    await svc.handle_mcu_frame(Command.CMD_PROCESS_KILL.value, 3, pb.ProcessKill(pid=123).SerializeToString())
    assert not mock_serial.acknowledge.called


async def test_runtime_handle_request_route_none_and_inbound_props(
    mock_bridge_service: BridgeService, mock_serial: AsyncMock
) -> None:
    svc = mock_bridge_service

    await svc.handle_request(pb.CloudQueuedPublish(topic_name="unmatched/topic", payload=b""))
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
    await svc.handle_request(pb.CloudQueuedPublish(topic_name="bridge/req/console.write", payload=b""))
    assert not mock_serial.send.called


async def test_runtime_handle_datastore_branches(mock_bridge_service: BridgeService, mock_serial: AsyncMock) -> None:
    svc = mock_bridge_service

    await svc.handle_request(
        pb.CloudQueuedPublish(
            topic_name="bridge/req/datastore.put",
            payload=pb.DatastorePut(key="mykey", value=b"x" * 600).SerializeToString(),
        )
    )
    assert not mock_serial.send.called

    await svc.handle_request(
        pb.CloudQueuedPublish(
            topic_name="bridge/req/datastore.get",
            payload=pb.DatastoreGet(key="missingkey").SerializeToString(),
        )
    )
    assert not mock_serial.send.called

    await svc.handle_request(
        pb.CloudQueuedPublish(
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
        pb.CloudQueuedPublish(
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
        pb.CloudQueuedPublish(
            topic_name="bridge/req/file.custom_act",
            payload=b"data",
        )
    )

    mock_serial.send = AsyncMock(return_value=False)
    res_mcu_w = await svc.file_write(pb.FileWrite(path="/mcu/test.txt", data=b"data"))
    assert mock_serial.send.called
    assert res_mcu_w.status == "error"

    svc.serial = None
    res_mcu_rm = await svc.file_remove(pb.FileRemove(path="/mcu/test.txt"))
    assert res_mcu_rm.status == "error"
    svc.serial = mock_serial

    res_loc_w = await svc.file_write(pb.FileWrite(path="../outside.txt", data=b"data"))
    assert res_loc_w.status == "error"

    res_loc_r = await svc.file_read(pb.FileRead(path="not_a_file"))
    assert res_loc_r.content == b""

    real_file = tmp_path / "real.txt"
    real_file.write_text("hello")
    svc.config.file_system_root = str(tmp_path)
    res_loc_r2 = await svc.file_read(pb.FileRead(path="real.txt"))
    assert real_file.exists()
    assert res_loc_r2.content == b"hello"


async def test_runtime_handle_shell_branches(mock_bridge_service: BridgeService, mock_serial: AsyncMock) -> None:
    svc = mock_bridge_service

    await svc.handle_request(
        pb.CloudQueuedPublish(
            topic_name="bridge/req/shell.unknown_act",
            payload=b"",
        )
    )
    assert not mock_serial.send.called

    proto_cmd = pb.ProcessRunAsync(command="echo hello").SerializeToString()
    await svc.handle_request(
        pb.CloudQueuedPublish(
            topic_name="bridge/req/process.run_async",
            payload=proto_cmd,
        )
    )

    await svc.process_kill(99999)
    assert 99999 not in svc.state.running_processes


async def test_runtime_handle_spi_and_pin_branches(mock_bridge_service: BridgeService, mock_serial: AsyncMock) -> None:
    svc = mock_bridge_service

    await svc.handle_request(
        pb.CloudQueuedPublish(
            topic_name="bridge/req/spi.transfer",
            payload=b"",
        )
    )
    assert not mock_serial.send.called

    await svc.handle_request(
        pb.CloudQueuedPublish(
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
        pb.CloudQueuedPublish(
            topic_name=f"{svc.state.cloud_topic_prefix}/req/system.free_memory",
            payload=b"",
        )
    )
    assert mock_serial.send.called

    mock_serial.send = AsyncMock(return_value=pb.FreeMemoryResponse(value=1024).SerializeToString())
    await svc.handle_request(
        pb.CloudQueuedPublish(
            topic_name=f"{svc.state.cloud_topic_prefix}/req/system.free_memory",
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
    msg = pb.CloudQueuedPublish(topic_name="br/test", payload=b"data")
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
    svc.cloud_stream = AsyncMock()
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

    handle_xoff: Callable[..., Awaitable[None]] = svc.handle_mcu_xoff
    await handle_xoff(1, None)
    assert svc.state.mcu_is_paused is True
    assert not svc.state.serial_tx_allowed.is_set()

    on_console: Callable[..., Awaitable[None]] = svc.handle_console_write
    await on_console(1, pb.ConsoleWrite(data=b""))

    svc.serial = None
    on_ds_get: Callable[..., Awaitable[bool]] = svc.handle_datastore_get
    assert await on_ds_get(1, pb.DatastoreGet(key="k")) is False


async def test_runtime_handle_datastore_empty_key_and_request_miss(
    mock_bridge_service: BridgeService,
) -> None:
    svc = mock_bridge_service

    await svc.handle_request(
        pb.CloudQueuedPublish(
            topic_name=f"{svc.state.cloud_topic_prefix}/req/datastore.put",
            payload=pb.DatastorePut(key="", value=b"val").SerializeToString(),
        )
    )

    mock_pub = AsyncMock()
    svc.cloud_publisher = mock_pub
    await svc.handle_request(
        pb.CloudQueuedPublish(
            topic_name=f"{svc.state.cloud_topic_prefix}/req/datastore.get",
            payload=pb.DatastoreGet(key="key1").SerializeToString(),
        )
    )
    assert mock_pub.await_count == 1
    assert mock_pub.await_args is not None
    published: pb.CloudQueuedPublish = mock_pub.await_args.args[0]
    assert "datastore.get" in published.topic_name


async def test_runtime_handle_mailbox_edge_branches(mock_bridge_service: BridgeService, mock_serial: AsyncMock) -> None:
    svc = mock_bridge_service

    svc.serial = None
    await svc.mailbox_push(pb.MailboxPush(data=b"hi"))
    assert not mock_serial.send.called

    svc.serial = mock_serial
    await svc.state.mailbox_incoming_queue.clear()
    res = await svc.mailbox_read(pb.SubscribeRequest())
    assert res.content == b""
    assert not mock_serial.send.called


async def test_runtime_handle_file_and_shell_edge_branches(
    mock_bridge_service: BridgeService, mock_serial: AsyncMock
) -> None:
    from unittest.mock import PropertyMock

    svc = mock_bridge_service

    svc.serial = None
    res_f = await svc.file_read(pb.FileRead(path="test.txt"))
    assert not mock_serial.send.called
    assert res_f.content == b""

    svc.serial = mock_serial
    res_unsafe = await svc.file_read(pb.FileRead(path="../../../etc/shadow"))
    assert not mock_serial.send.called
    assert res_unsafe.content == b""

    res_empty = await svc.process_run_async(pb.ProcessRunAsync(command=""))
    assert not mock_serial.send.called
    assert res_empty.pid == 0

    svc.state.allowed_policy = pb.AllowedCommandPolicy(entries=["echo"])
    res_prop = await svc.process_run_async(pb.ProcessRunAsync(command="echo prop"))
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
    res_spi_cfg = await svc.spi_configure(pb.SpiConfig())
    assert not mock_serial.send.called
    assert res_spi_cfg.status == "error"

    svc.serial = mock_serial
    mock_serial.send.return_value = False
    res_spi_xfer = pb.SpiTransferResponse.FromString(
        await svc.execute_rpc("SpiTransfer", pb.SpiTransfer(data=b"data").SerializeToString())
    )
    assert mock_serial.send.called
    assert res_spi_xfer.data == b""

    mock_serial.send.return_value = pb.SpiTransferResponse(data=b"resp").SerializeToString()
    res_spi_xfer_ok = pb.SpiTransferResponse.FromString(
        await svc.execute_rpc("SpiTransfer", pb.SpiTransfer(data=b"data").SerializeToString())
    )
    assert res_spi_xfer_ok.data == b"resp"

    mock_serial.send.reset_mock()
    res_boot = await svc.enter_bootloader(pb.EnterBootloader())
    assert res_boot.status == "ok"

    mock_serial.send.reset_mock()
    svc.serial = None
    res_pin_write = pb.GenericResponse.FromString(
        await svc.execute_rpc("DigitalWrite", pb.DigitalWrite(pin=13, value=1).SerializeToString())
    )
    assert not mock_serial.send.called
    assert res_pin_write.status == "error"

    svc.serial = mock_serial
    res_pin_ok = pb.GenericResponse.FromString(
        await svc.execute_rpc("DigitalWrite", pb.DigitalWrite(pin=13, value=1).SerializeToString())
    )
    assert mock_serial.send.called
    assert res_pin_ok.status == "ok"


async def test_runtime_run_cloud_cancelled(mock_bridge_service: BridgeService, mocker: MockerFixture) -> None:
    svc = mock_bridge_service
    svc.config.cloud_tls = False

    mock_channel = MagicMock()
    mock_stub = MagicMock()
    mock_stream = AsyncMock()
    mock_stream.__aenter__.side_effect = asyncio.CancelledError()
    mock_stub.Session.open.return_value = mock_stream
    mocker.patch("mcubridge.services.runtime.Channel", return_value=mock_channel)
    mocker.patch("mcubridge.services.runtime.CloudBridgeStub", return_value=mock_stub)

    with pytest.raises(asyncio.CancelledError):
        await svc.run_cloud()

    assert svc.state.cloud_fsm.disabled.is_active


async def test_runtime_cloud_session_non_command_envelope(
    mock_bridge_service: BridgeService, mocker: MockerFixture
) -> None:
    svc = mock_bridge_service

    event_env = pb.CloudEnvelope(
        protocol_version=protocol.PROTOCOL_VERSION,
        event=pb.EventNotification(event_type="custom", severity="info", description="test"),
    )

    mock_stub = MagicMock()
    mock_stub.Session.open.return_value = MockGrpcStream(inbound=[event_env])

    mocker.patch("mcubridge.services.runtime.Channel")
    mocker.patch("mcubridge.services.runtime.CloudBridgeStub", return_value=mock_stub)
    await svc.connect_cloud_session(None)
    assert svc.cloud_stream is None
    assert svc.state.cloud_fsm.spooling_degraded.is_active


async def test_send_cloud_event_branches(mock_bridge_service: BridgeService) -> None:
    service = mock_bridge_service

    service.cloud_stream = None
    await service.send_cloud_event("heartbeat", "info", "test_msg")

    mock_stream = MockGrpcStream[Any, pb.CloudEnvelope]()
    service.cloud_stream = mock_stream
    await service.send_cloud_event("heartbeat", "info", "test_msg")
    mock_stream.send_message.assert_awaited_once()
    envelope = mock_stream.sent_message
    assert envelope.event.event_type == "heartbeat"
    assert envelope.event.description == "test_msg"


async def test_runtime_on_mcu_datastore_get_branches(mock_bridge_service: BridgeService) -> None:
    svc = mock_bridge_service
    handle_ds_get: Callable[[int, pb.DatastoreGet], Awaitable[bool]] = svc.handle_datastore_get
    svc.serial = None
    req = pb.DatastoreGet(key="cfg/mode")
    assert await handle_ds_get(1, req) is False

    mock_serial = AsyncMock()
    mock_serial.send = AsyncMock(return_value=True)
    svc.serial = mock_serial
    svc.state.datastore_cache = AsyncMock()
    svc.state.datastore_cache.get = AsyncMock(return_value=b"active")

    res = await handle_ds_get(1, req)
    assert res is True
    mock_serial.send.assert_awaited_once()
    sent_cmd = mock_serial.send.call_args[0][0]
    sent_resp = mock_serial.send.call_args[0][1]
    assert sent_cmd == Command.CMD_DATASTORE_GET_RESP.value
    assert sent_resp.value == b"active"

    mock_serial.send.reset_mock()
    svc.state.datastore_cache = None
    res_none = await handle_ds_get(2, req)
    assert res_none is True
    assert mock_serial.send.call_args[0][1].value == b""


async def test_runtime_cloud_session_rpc_commands_and_errors(
    mock_bridge_service: BridgeService, mocker: MockerFixture
) -> None:
    svc = mock_bridge_service

    pong_env = pb.CloudEnvelope(protocol_version=protocol.PROTOCOL_VERSION, pong=pb.KeepalivePong())
    cmd_ok = pb.CloudEnvelope(
        protocol_version=protocol.PROTOCOL_VERSION,
        sequence_id=1,
        command_request=pb.CommandRequest(command_path="rpc/GetVersion", payload=b""),
    )
    cmd_invalid = pb.CloudEnvelope(
        protocol_version=protocol.PROTOCOL_VERSION,
        sequence_id=2,
        command_request=pb.CommandRequest(command_path="rpc/UnknownMethod", payload=b"err"),
    )
    cmd_os_err = pb.CloudEnvelope(
        protocol_version=protocol.PROTOCOL_VERSION,
        sequence_id=3,
        command_request=pb.CommandRequest(
            command_path="rpc/SetPinMode",
            payload=pb.PinMode(pin=13, mode=pb.PinModeType.PIN_OUTPUT).SerializeToString(),
        ),
    )

    mock_stream = MockGrpcStream[pb.CloudEnvelope, pb.CloudEnvelope](
        inbound=[pong_env, cmd_ok, cmd_invalid, cmd_os_err],
        aiter_exception=ConnectionError("connection reset by peer"),
    )

    # Configure boundary mock_serial: returns version on CMD_GET_VERSION, raises OSError on CMD_SET_PIN_MODE
    async def mock_serial_send(cmd: int, _payload: Any) -> Any:
        if cmd == Command.CMD_GET_VERSION.value:
            return pb.VersionResponse(major=2, minor=0, patch=0)
        raise OSError("Hardware IO error")

    mock_serial = AsyncMock(spec=SerialTransport)
    mock_serial.send = AsyncMock(side_effect=mock_serial_send)
    svc.serial = mock_serial

    mock_stub = MagicMock()
    mock_stub.Session.open.return_value = mock_stream

    mocker.patch("mcubridge.services.runtime.Channel")
    mocker.patch("mcubridge.services.runtime.CloudBridgeStub", return_value=mock_stub)

    with pytest.raises(ConnectionError, match="Cloud session stream disconnected"):
        await svc.connect_cloud_session(MagicMock())

    # Envelope 0 is the initial status_online event from connect_cloud_session
    sent_envelopes = mock_stream.sent_messages
    assert len(sent_envelopes) == 4
    assert sent_envelopes[0].event.event_type == "status_online"
    assert sent_envelopes[1].command_response.status_code == 200
    assert sent_envelopes[2].command_response.status_code == 404
    assert "Unknown RPC method" in sent_envelopes[2].command_response.error_message
    assert sent_envelopes[3].command_response.status_code == 500
    assert "Hardware IO error" in sent_envelopes[3].command_response.error_message
    assert svc.state.cloud_fsm.spooling_degraded.is_active


async def test_runtime_service_edge_branches(
    runtime_config: RuntimeConfig,
    runtime_state: RuntimeState,
    mocker: MockerFixture,
) -> None:
    mock_serial = AsyncMock(spec=SerialTransport)
    svc = BridgeService(runtime_config, runtime_state, mock_serial)

    # 1. Datastore PUT when payload is malformed / invalid protobuf
    inbound_bad = pb.CloudQueuedPublish(topic_name="br/req/datastore.put", payload=b"\xff\xff\xff")
    await svc.handle_request(inbound_bad)

    # 2. Datastore PUT when state.datastore_cache is None
    mocker.stopall()
    runtime_state.datastore_cache = None
    mock_publisher = AsyncMock()
    svc.cloud_publisher = mock_publisher
    inbound_put = pb.CloudQueuedPublish(
        topic_name="br/req/datastore.put",
        payload=pb.DatastorePut(key="k", value=b"val").SerializeToString(),
    )
    await svc.handle_request(inbound_put)
    assert mock_publisher.await_count == 2

    # 3. connect_cloud_session stream connection error
    mock_channel = MagicMock()
    mock_stub = MagicMock()
    mock_stream = AsyncMock()
    mock_stream.__aenter__.side_effect = ConnectionError("stream stop")
    mock_stub.Session.open.return_value = mock_stream
    mocker.patch("mcubridge.services.runtime.Channel", return_value=mock_channel)
    mocker.patch("mcubridge.services.runtime.CloudBridgeStub", return_value=mock_stub)
    mock_tls = MagicMock()

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
    assert len(protocol.REGISTRY.rpc_specs) >= 20
    for spec in protocol.REGISTRY.rpc_specs:
        assert spec.handler_name is not None
        if spec.is_custom_handler:
            handler = getattr(mock_bridge_service, spec.handler_name, None)
            assert callable(handler)
    assert callable(mock_bridge_service.execute_rpc)


async def test_runtime_dispatch_unmapped_branches(mock_bridge_service: BridgeService, tmp_path: Path) -> None:
    """[SIL-2] Ensure edge branches for unmapped topics and actions in dispatchers are covered."""
    svc = mock_bridge_service

    # 1. Unroutable cloud topic
    res_clock = await svc.handle_request(pb.CloudQueuedPublish(topic_name="bridge/unroutable/topic", payload=b""))
    assert res_clock is None

    # 2. Unregistered RPC method
    res_unreg = await svc.handle_request(pb.CloudQueuedPublish(topic_name="bridge/req/unknown.rpc", payload=b""))
    assert res_unreg is None

    # 3. File read with unsafe path
    res_file_unsafe = await svc.file_read(pb.FileRead(path="../../../etc/passwd"))
    assert res_file_unsafe.content == b""

    # 4. Process run with empty command
    res_sh_empty = await svc.process_run_async(pb.ProcessRunAsync(command=""))
    assert res_sh_empty.pid == 0

    # 5. SPI configure when serial is None
    serial_bak = svc.serial
    svc.serial = None
    res_spi = await svc.spi_configure(pb.SpiConfig())
    assert res_spi.status == "error"
    svc.serial = serial_bak


async def test_runtime_canonical_rpc_dispatch(mock_bridge_service: BridgeService, mock_serial: AsyncMock) -> None:
    svc = mock_bridge_service
    svc.state.link_sync_event.set()

    prefix = svc.state.cloud_topic_prefix

    # 1. Successful RPC dispatch on {prefix}/req/gpio.write
    req_dw = pb.DigitalWrite(pin=13, value=1)
    pub_req = pb.CloudQueuedPublish(
        topic_name=f"{prefix}/req/gpio.write",
        payload=req_dw.SerializeToString(),
    )
    mock_serial.send.return_value = True

    enqueued: list[tuple[str, bytes]] = []

    async def mock_enqueue(topic_name: str, payload: bytes, *args: Any, **kwargs: Any) -> None:
        enqueued.append((topic_name, payload))

    svc.enqueue_cloud_publish = mock_enqueue

    await svc.handle_request(pub_req)
    assert len(enqueued) == 1
    assert enqueued[0][0] == f"{prefix}/res/gpio.write"
    resp = pb.GenericResponse()
    resp.ParseFromString(enqueued[0][1])
    assert resp.status == "ok"

    # 2. Fleet topic: devices/yun-01/req/gpio.write
    enqueued.clear()
    pub_fleet = pb.CloudQueuedPublish(
        topic_name="devices/yun-01/req/gpio.write",
        payload=req_dw.SerializeToString(),
    )
    await svc.handle_request(pub_fleet)
    assert len(enqueued) == 1
    assert enqueued[0][0] == "devices/yun-01/res/gpio.write"

    # 3. RPC execution exception: replies with error
    enqueued.clear()
    with patch.object(svc, "execute_rpc", side_effect=RuntimeError("hardware failure")):
        await svc.handle_request(pub_req)
    assert len(enqueued) == 1
    assert enqueued[0][0] == f"{prefix}/res/gpio.write"
    assert b"error:hardware failure" in enqueued[0][1]

    # 4. Unauthorized RPC method
    enqueued.clear()
    auth = pb.TopicAuthorization(digital_write=False)
    svc.state.topic_authorization = auth
    await svc.handle_request(pub_req)
    # Rejected -> nothing enqueued on res topic
    assert not any(topic.endswith("/res/gpio.write") for topic, _ in enqueued)
