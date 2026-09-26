"""Tests for runtime service lifecycle and basic transport integration."""

from __future__ import annotations

import asyncio
import time
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import Any, cast
from unittest.mock import AsyncMock, MagicMock

import lmdb
import pytest
from hypothesis import given
from hypothesis import strategies as st
from mcubridge.config.settings import RuntimeConfig
from mcubridge.protocol import mcubridge_pb2 as pb
from mcubridge.protocol import protocol
from mcubridge.protocol.protocol import (
    Command,
    DatastoreAction,
    ShellAction,
    SpiAction,
    Status,
    SystemAction,
    Topic,
)
from mcubridge.protocol.structures import TopicRoute
from mcubridge.services.runtime import BridgeService
from mcubridge.state.context import ProcessContext, create_runtime_state
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


@pytest.mark.asyncio
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


@pytest.mark.asyncio
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


@pytest.mark.asyncio
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


@pytest.mark.asyncio
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

        mock_msg = PublishPacket("br/console/in", b"hello")

        await service.handle_request(mock_msg)

        mock_serial.send.assert_called()
    finally:
        if service is not None:
            service.cleanup()
        else:
            state.cleanup()


@pytest.mark.asyncio
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
        object.__setattr__(service, "_cloud_stream", mock_stream)
        await service.flush_cloud_spool()

        mock_stream.send_message.assert_called_once()
        assert state.cloud_spool_pending_messages == 0
    finally:
        if service is not None:
            service.cleanup()
        else:
            state.cleanup()


@pytest.mark.asyncio
async def test_handle_cloud_pin_overflow_reports_error(mocker: MockerFixture) -> None:
    service = None
    config = _make_config()
    state = create_runtime_state(config)
    try:
        from mcubridge.protocol.structures import PendingPinRequest

        mock_serial = AsyncMock(spec=SerialTransport)
        service = BridgeService(config, state, mock_serial)
        state.state = "synchronized"
        state.link_sync_event.set()
        state.pending_pin_request_limit = 1
        state.pending_digital_reads.append(PendingPinRequest(pin=13, reply_context=None))

        captured: list[pb.CloudQueuedPublish] = []

        async def capture_enqueue(message: pb.CloudQueuedPublish, *, reply_context: object | None = None) -> None:
            del reply_context
            captured.append(message)

        mocker.patch.object(service, "enqueue_cloud", side_effect=capture_enqueue)

        class PublishPacket:
            def __init__(self, topic: str, payload: bytes) -> None:
                self.topic = topic
                self.payload = payload
                self.properties = None

        message = PublishPacket("br/d/13/read", b"")

        await service.handle_request(message)

        assert captured
        assert any(
            isinstance(prop, pb.UserProperty) and prop.key == "bridge-error" and prop.value == "pending-pin-overflow"
            for prop in captured[0].user_properties
        )
        mock_serial.send.assert_not_called()
    finally:
        if service is not None:
            service.cleanup()
        else:
            state.cleanup()


@pytest.mark.asyncio
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

    res = await svc.poll_process(8888)
    assert res.finished is False
    assert res.stdout_truncated is True


@pytest.mark.asyncio
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

    class AsyncStreamMock:
        def __init__(self) -> None:
            self.send_message = AsyncMock()

        def __aiter__(self):
            async def _gen():
                yield envelope_pong
                yield envelope_cmd

            return _gen()

    mock_stream = AsyncStreamMock()
    mock_open_ctx = AsyncMock()
    mock_open_ctx.__aenter__.return_value = mock_stream
    mock_open_ctx.__aexit__.return_value = None

    mocker.patch("mcubridge.services.runtime.Channel")
    mock_stub_cls = mocker.patch("mcubridge.services.runtime.CloudBridgeStub")
    mocker.patch.object(svc, "_send_cloud_event", new_callable=AsyncMock)
    mocker.patch.object(svc, "flush_cloud_spool", new_callable=AsyncMock)

    mock_stub = MagicMock()
    mock_stub.Session.open.return_value = mock_open_ctx
    mock_stub_cls.return_value = mock_stub

    await svc.connect_cloud_session(None)
    assert svc.state.connected_via_http3 is True
    mock_stream.send_message.assert_awaited_once()
    resp = mock_stream.send_message.call_args[0][0]
    assert resp.sequence_id == 1234
    assert resp.command_response.status_code == 200


@pytest.mark.asyncio
async def test_process_terminate_sigkill_escalation(mocker: MockerFixture) -> None:
    mock_ctx = MagicMock()
    mock_ctx.handle.returncode = None
    mock_ctx.handle.pid = 999999

    mock_term = mocker.patch("mcubridge.services.runtime.terminate_pid_tree")
    terminate_proc: Callable[..., Awaitable[int]] = getattr(BridgeService, "_terminate_process")
    code = await terminate_proc(MagicMock(), 999999, mock_ctx, grace_period=0.5)
    mock_term.assert_called_once_with(999999, timeout=0.5)
    assert code == -1


@pytest.mark.asyncio
async def test_runtime_service_run_and_teardown_exceptions(mock_bridge_service: BridgeService) -> None:
    service = mock_bridge_service
    state = service.state

    mock_spool = AsyncMock(spec=LmdbDeque)
    mock_spool.close.side_effect = OSError("spool close error")
    setattr(service, "_cloud_spool", mock_spool)

    mock_cache = AsyncMock()
    mock_cache.close.side_effect = lmdb.Error("db error")
    state.datastore_cache = mock_cache

    run_task = asyncio.create_task(service.run())
    await asyncio.sleep(0.02)
    run_task.cancel()

    await run_task
    assert run_task.done()
    assert getattr(service, "_cloud_spool") is None


@pytest.mark.asyncio
async def test_runtime_run_cloud_disabled(mock_bridge_service: BridgeService, mocker: MockerFixture) -> None:
    service = mock_bridge_service
    service.config.cloud_enabled = False

    mock_info = mocker.patch("mcubridge.services.runtime.logger.info")
    await service.run_cloud()
    assert mock_info.called


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

        route_put = TopicRoute(
            raw=f"{cfg.topic_prefix}/datastore/put/{key}",
            prefix=cfg.topic_prefix,
            topic=Topic.DATASTORE,
            segments=("put", key),
        )
        handle_datastore: Callable[[TopicRoute, pb.CloudQueuedPublish], Awaitable[None]] = getattr(
            service, "_handle_datastore"
        )
        inbound_put = pb.CloudQueuedPublish(topic_name=f"{cfg.topic_prefix}/datastore/put/{key}", payload=value)
        await handle_datastore(route_put, inbound_put)
        assert await state.datastore_cache.get(key) == value

        route_get_hit = TopicRoute(
            raw=f"{cfg.topic_prefix}/datastore/get/{key}",
            prefix=cfg.topic_prefix,
            topic=Topic.DATASTORE,
            segments=("get", key),
        )
        inbound_get = pb.CloudQueuedPublish(topic_name=f"{cfg.topic_prefix}/datastore/get/{key}", payload=b"")
        mock_enqueue = AsyncMock()
        service.enqueue_cloud = mock_enqueue
        await handle_datastore(route_get_hit, inbound_get)
        assert mock_enqueue.await_count == 1

        route_get_miss = TopicRoute(
            raw=f"{cfg.topic_prefix}/datastore/get/non_existing_{key}/request",
            prefix=cfg.topic_prefix,
            topic=Topic.DATASTORE,
            segments=("get", f"non_existing_{key}", "request"),
        )
        mock_enqueue.reset_mock()
        await handle_datastore(route_get_miss, inbound_get)
        assert mock_enqueue.await_count == 1

        state.cleanup()

    asyncio.run(_run())


@pytest.mark.asyncio
async def test_runtime_handle_mcu_status_binary_undecodable(
    mock_bridge_service: BridgeService, mocker: MockerFixture
) -> None:
    service = mock_bridge_service
    mock_enqueue = mocker.patch.object(service, "enqueue_cloud", new_callable=AsyncMock)
    handle_mcu_status: Callable[..., Awaitable[None]] = getattr(service, "_handle_mcu_status")
    await handle_mcu_status(Status.TIMEOUT, 1, b"\xff\xfe\xfd\x80")
    assert mock_enqueue.call_count == 1
    await handle_mcu_status(Status.ERROR, 2, cast(Any, 12345))
    assert mock_enqueue.call_count == 2


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
        inbound = pb.CloudQueuedPublish(topic_name=f"{cfg.topic_prefix}/system/version/get", payload=b"")

        req_mcu_version: Callable[[pb.CloudQueuedPublish], Awaitable[bool]] = getattr(service, "_request_mcu_version")
        res = await req_mcu_version(inbound)
        assert res is True
        assert state.mcu_version == (major, minor, patch_ver)

        route_ver = TopicRoute(
            raw=f"{cfg.topic_prefix}/system/version/get",
            prefix=cfg.topic_prefix,
            topic=Topic.SYSTEM,
            segments=("version", "get"),
        )
        handle_system: Callable[[TopicRoute, pb.CloudQueuedPublish], Awaitable[None]] = getattr(
            service, "_handle_system"
        )
        await handle_system(route_ver, inbound)
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
        mock_serial = AsyncMock(spec=SerialTransport)
        mock_serial.send = AsyncMock(return_value=True)
        service = BridgeService(cfg, state, mock_serial)

        handle_pin: Callable[[TopicRoute, pb.CloudQueuedPublish], Awaitable[None]] = getattr(service, "_handle_pin")

        route_aw = TopicRoute(
            raw=f"{cfg.topic_prefix}/a/{pin}",
            prefix=cfg.topic_prefix,
            topic=Topic.ANALOG,
            segments=(str(pin),),
        )
        inbound_aw = pb.CloudQueuedPublish(topic_name=f"{cfg.topic_prefix}/a/{pin}", payload=str(analog_val).encode())
        await handle_pin(route_aw, inbound_aw)
        mock_serial.send.assert_called_with(Command.CMD_ANALOG_WRITE.value, pb.AnalogWrite(pin=pin, value=analog_val))

        route_dw = TopicRoute(
            raw=f"{cfg.topic_prefix}/d/{pin}",
            prefix=cfg.topic_prefix,
            topic=Topic.DIGITAL,
            segments=(str(pin),),
        )
        inbound_dw = pb.CloudQueuedPublish(topic_name=f"{cfg.topic_prefix}/d/{pin}", payload=non_digit.encode())
        await handle_pin(route_dw, inbound_dw)
        mock_serial.send.assert_called_with(Command.CMD_DIGITAL_WRITE.value, pb.DigitalWrite(pin=pin, value=0))

        state.cleanup()

    asyncio.run(_run())


@pytest.mark.asyncio
async def test_flush_cloud_spool_corrupt_and_index_error(
    mock_bridge_service: BridgeService, mocker: MockerFixture
) -> None:
    svc = mock_bridge_service
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
async def test_runtime_terminate_process_escalation(mock_bridge_service: BridgeService, mocker: MockerFixture) -> None:
    svc = mock_bridge_service

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
async def test_runtime_flush_console_queue_send_failed(
    mock_bridge_service: BridgeService, mock_serial: AsyncMock
) -> None:
    mock_serial.send.return_value = False
    svc = mock_bridge_service

    svc.state.console_to_mcu_queue.append(b"console payload")
    flush_console_fn: Callable[[], Awaitable[None]] = getattr(svc, "_flush_console_queue")
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

        reject_cloud_fn: Callable[..., Awaitable[None]] = getattr(BridgeService, "_reject_cloud")
        await reject_cloud_fn(svc, pb.CloudQueuedPublish(), Topic.DIGITAL, action)
        svc.enqueue_cloud_status_report.assert_awaited_once()

        svc.enqueue_cloud_status_report.reset_mock()
        await reject_cloud_fn(svc, pb.CloudQueuedPublish(), topic_str, action)
        svc.enqueue_cloud_status_report.assert_awaited_once()

    asyncio.run(_run())


@pytest.mark.asyncio
async def test_runtime_cloud_spool_trimming_and_drop(mock_bridge_service: BridgeService) -> None:
    svc = mock_bridge_service

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
    mock_bridge_service: BridgeService,
) -> None:
    svc = mock_bridge_service
    svc.ubus_service = MagicMock()
    svc.cleanup()
    assert svc.serial is None
    assert svc.ubus_service.stop.called


@pytest.mark.asyncio
async def test_runtime_file_dispatch_local_methods_none_path(
    runtime_config: RuntimeConfig, mock_bridge_service: BridgeService, mock_serial: AsyncMock
) -> None:
    svc = mock_bridge_service
    req = pb.CloudQueuedPublish(topic_name="bridge/file/read/test", payload=b"")

    route = TopicRoute(
        raw="bridge/file/read/../../secret",
        prefix=runtime_config.topic_prefix,
        topic=Topic.FILE,
        segments=("read", "..", "..", "secret"),
    )
    handle_file: Callable[..., Awaitable[None]] = getattr(svc, "_handle_file")
    await handle_file(route, req)
    assert mock_serial.send.call_count == 0


@pytest.mark.asyncio
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


@pytest.mark.asyncio
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


@pytest.mark.asyncio
async def test_runtime_handle_console_empty_payload(mock_bridge_service: BridgeService, mock_serial: AsyncMock) -> None:
    svc = mock_bridge_service
    handle_console: Callable[..., Awaitable[None]] = getattr(svc, "_handle_console")
    await handle_console(None, pb.CloudQueuedPublish(topic_name="bridge/console", payload=b""))
    assert not mock_serial.send.called


@pytest.mark.asyncio
async def test_runtime_handle_datastore_branches(mock_bridge_service: BridgeService, mock_serial: AsyncMock) -> None:
    svc = mock_bridge_service
    handle_ds: Callable[..., Awaitable[None]] = getattr(svc, "_handle_datastore")

    route_put = TopicRoute(
        raw="", prefix="bridge", topic=Topic.DATASTORE, segments=(DatastoreAction.PUT.value, "mykey")
    )
    await handle_ds(route_put, pb.CloudQueuedPublish(payload=b"x" * 600))
    assert not mock_serial.send.called

    route_get = TopicRoute(
        raw="", prefix="bridge", topic=Topic.DATASTORE, segments=(DatastoreAction.GET.value, "missingkey")
    )
    await handle_ds(route_get, pb.CloudQueuedPublish(payload=b""))
    assert not mock_serial.send.called

    route_unknown = TopicRoute(raw="", prefix="bridge", topic=Topic.DATASTORE, segments=("unknown_act", "key"))
    await handle_ds(route_unknown, pb.CloudQueuedPublish(payload=b""))
    assert not mock_serial.send.called


@pytest.mark.asyncio
async def test_runtime_handle_mailbox_unknown_identifier(
    mock_bridge_service: BridgeService, mock_serial: AsyncMock
) -> None:
    svc = mock_bridge_service
    route = TopicRoute(raw="", prefix="bridge", topic=Topic.MAILBOX, segments=("unknown",))
    handle_mb: Callable[..., Awaitable[None]] = getattr(svc, "_handle_mailbox")
    await handle_mb(route, pb.CloudQueuedPublish(payload=b"test"))
    assert not mock_serial.send.called


@pytest.mark.asyncio
async def test_runtime_handle_file_unhandled_action_and_failed_writes(
    mock_bridge_service: BridgeService, mock_serial: AsyncMock, tmp_path: Path, mocker: MockerFixture
) -> None:
    svc = mock_bridge_service
    handle_file: Callable[..., Awaitable[None]] = getattr(svc, "_handle_file")
    h_mcu_w: Callable[..., Awaitable[bool]] = getattr(svc, "_handle_file_mcu_write")
    h_mcu_rm: Callable[..., Awaitable[bool]] = getattr(svc, "_handle_file_mcu_remove")
    h_loc_w: Callable[..., Awaitable[None]] = getattr(svc, "_handle_file_local_write")
    h_loc_r: Callable[..., Awaitable[None]] = getattr(svc, "_handle_file_local_read")

    route_unhandled = TopicRoute(raw="", prefix="bridge", topic=Topic.FILE, segments=("custom_act", "mcu/file"))
    await handle_file(route_unhandled, pb.CloudQueuedPublish(payload=b"data"))

    mock_serial.send = AsyncMock(return_value=False)
    await h_mcu_w("mcu/test.txt", pb.CloudQueuedPublish(payload=b"data"))
    assert mock_serial.send.called

    svc.serial = None
    await h_mcu_rm("mcu/test.txt", pb.CloudQueuedPublish(payload=b""))
    svc.serial = mock_serial

    mocker.patch.object(svc, "_write_with_quota", return_value=False)
    await h_loc_w("f.txt", pb.CloudQueuedPublish(payload=b"data"))

    await h_loc_r("not_a_file", pb.CloudQueuedPublish(topic_name="bridge/file/read"))

    real_file = tmp_path / "real.txt"
    real_file.write_text("hello")
    await h_loc_r("real.txt", pb.CloudQueuedPublish(topic_name="bridge/file/read/response"))
    assert real_file.exists()


@pytest.mark.asyncio
async def test_runtime_handle_shell_branches(
    mock_bridge_service: BridgeService, mock_serial: AsyncMock, mocker: MockerFixture
) -> None:
    svc = mock_bridge_service
    handle_shell: Callable[..., Awaitable[None]] = getattr(svc, "_handle_shell")

    route_unregistered = TopicRoute(raw="", prefix="bridge", topic=Topic.SHELL, segments=("unknown_act",))
    await handle_shell(route_unregistered, pb.CloudQueuedPublish(payload=b""))
    assert not mock_serial.send.called

    proto_cmd = pb.ProcessRunAsync(command="echo hello").SerializeToString()
    route_run = TopicRoute(raw="", prefix="bridge", topic=Topic.SHELL, segments=(ShellAction.RUN_ASYNC.value,))
    mock_run = mocker.patch.object(svc, "run_process", new_callable=AsyncMock, return_value=123)
    await handle_shell(route_run, pb.CloudQueuedPublish(payload=proto_cmd))
    assert mock_run.called

    await svc.kill_process(99999)
    assert 99999 not in svc.state.running_processes


@pytest.mark.asyncio
async def test_runtime_handle_spi_and_pin_branches(mock_bridge_service: BridgeService, mock_serial: AsyncMock) -> None:
    svc = mock_bridge_service
    handle_spi: Callable[..., Awaitable[None]] = getattr(svc, "_handle_spi")
    handle_pin: Callable[..., Awaitable[None]] = getattr(svc, "_handle_pin")

    route_spi_xfer = TopicRoute(raw="", prefix="bridge", topic=Topic.SPI, segments=(SpiAction.TRANSFER.value,))
    await handle_spi(route_spi_xfer, pb.CloudQueuedPublish(payload=b""))
    assert not mock_serial.send.called

    route_pin = TopicRoute(raw="", prefix="bridge", topic=Topic.DIGITAL, segments=("13", "unknown_action"))
    await handle_pin(route_pin, pb.CloudQueuedPublish(payload=b"1"))
    assert not mock_serial.send.called


@pytest.mark.asyncio
async def test_runtime_handle_system_free_memory_non_bytes(
    mock_bridge_service: BridgeService, mock_serial: AsyncMock
) -> None:
    mock_serial.send = AsyncMock(return_value=False)
    svc = mock_bridge_service
    handle_sys: Callable[..., Awaitable[None]] = getattr(svc, "_handle_system")

    route = TopicRoute(raw="", prefix="bridge", topic=Topic.SYSTEM, segments=(SystemAction.FREE_MEMORY.value, "get"))
    await handle_sys(route, pb.CloudQueuedPublish(payload=b""))
    assert mock_serial.send.called


@pytest.mark.asyncio
async def test_runtime_cloud_spool_locked_limit_errors(
    mock_bridge_service: BridgeService,
) -> None:
    svc = mock_bridge_service
    svc.state.cloud_queue_limit = 1
    spool_locked: Callable[..., Awaitable[bool]] = getattr(svc, "_spool_cloud_message_locked")

    mock_spool = MagicMock()
    mock_spool.__len__.side_effect = [2, 0]
    mock_spool.popleft = AsyncMock(side_effect=IndexError("empty"))
    mock_spool.append = AsyncMock(return_value=None)
    setattr(svc, "_cloud_spool", mock_spool)
    msg = pb.CloudQueuedPublish(topic_name="br/test", payload=b"data")
    assert await spool_locked(msg) is True

    mock_spool = MagicMock()
    mock_spool.__len__.side_effect = [2, 0]
    mock_spool.popleft = AsyncMock(side_effect=OSError("IO failure"))
    mock_spool.append = AsyncMock(return_value=None)
    setattr(svc, "_cloud_spool", mock_spool)
    assert await spool_locked(msg) is True

    mock_spool = MagicMock()
    mock_spool.__len__.side_effect = [0, 0]
    mock_spool.append = AsyncMock(side_effect=OSError("Disk full"))
    setattr(svc, "_cloud_spool", mock_spool)
    assert await spool_locked(msg) is False


@pytest.mark.asyncio
async def test_runtime_flush_cloud_spool_corrupt_and_errors(
    mock_bridge_service: BridgeService,
) -> None:
    svc = mock_bridge_service
    setattr(svc, "_cloud_stream", AsyncMock())
    flush_locked: Callable[[], Awaitable[None]] = getattr(svc, "_flush_cloud_spool_locked")

    len_val = 1
    mock_spool = MagicMock()
    mock_spool.__len__.side_effect = lambda: len_val
    mock_spool.peek = AsyncMock(return_value=b"corrupt-data")
    mock_spool.popleft = AsyncMock(side_effect=IndexError("empty"))
    mock_spool.vacuum = AsyncMock()
    setattr(svc, "_cloud_spool", mock_spool)
    await flush_locked()
    mock_spool.popleft.assert_awaited_once()

    len_val = 1
    mock_spool = MagicMock()
    mock_spool.__len__.side_effect = lambda: len_val
    mock_spool.peek = AsyncMock(return_value=b"corrupt-data")
    mock_spool.popleft = AsyncMock(side_effect=OSError("IO Error"))
    mock_spool.vacuum = AsyncMock()
    setattr(svc, "_cloud_spool", mock_spool)
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
    setattr(svc, "_cloud_spool", mock_spool)
    initial_dropped = svc.state.cloud_spool_corrupt_dropped
    await flush_locked()
    mock_spool.popleft.assert_awaited_once()
    assert svc.state.cloud_spool_corrupt_dropped == initial_dropped + 1


@pytest.mark.asyncio
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
    resp = await svc.poll_process(555)
    assert resp.finished is True
    assert 555 not in svc.state.running_processes

    handle_xoff: Callable[..., Awaitable[None]] = getattr(svc, "_handle_mcu_xoff")
    await handle_xoff(1, None)
    assert svc.state.mcu_is_paused is True
    assert not svc.state.serial_tx_allowed.is_set()

    on_console: Callable[..., Awaitable[None]] = getattr(svc, "_on_mcu_console_write")
    await on_console(1, pb.ConsoleWrite(data=b""))

    svc.serial = None
    on_ds_get: Callable[..., Awaitable[bool]] = getattr(svc, "_on_mcu_datastore_get")
    assert await on_ds_get(1, pb.DatastoreGet(key="k")) is False


@pytest.mark.asyncio
async def test_runtime_handle_datastore_empty_key_and_request_miss(
    mock_bridge_service: BridgeService, mocker: MockerFixture
) -> None:
    svc = mock_bridge_service
    handle_ds: Callable[..., Awaitable[None]] = getattr(svc, "_handle_datastore")

    route_empty = TopicRoute(raw="", prefix="bridge", topic=Topic.DATASTORE, segments=(DatastoreAction.PUT.value,))
    await handle_ds(route_empty, pb.CloudQueuedPublish(payload=b"val"))

    route_req = TopicRoute(
        raw="", prefix="bridge", topic=Topic.DATASTORE, segments=(DatastoreAction.GET.value, "key1", "request")
    )
    mock_pub = mocker.patch.object(svc, "publish_datastore_value", new_callable=AsyncMock)
    await handle_ds(route_req, pb.CloudQueuedPublish(payload=b""))
    assert mock_pub.call_count == 1
    assert mock_pub.call_args[0][0] == "key1/request"
    assert mock_pub.call_args[1]["error"] == "datastore-miss"


@pytest.mark.asyncio
async def test_runtime_handle_mailbox_edge_branches(mock_bridge_service: BridgeService, mock_serial: AsyncMock) -> None:
    svc = mock_bridge_service
    handle_mb: Callable[..., Awaitable[None]] = getattr(svc, "_handle_mailbox")

    svc.serial = None
    route = TopicRoute(raw="", prefix="bridge", topic=Topic.MAILBOX, segments=("write",))
    await handle_mb(route, pb.CloudQueuedPublish(payload=b"hi"))
    assert not mock_serial.send.called

    svc.serial = mock_serial
    await svc.state.mailbox_incoming_queue.clear()
    route_read = TopicRoute(raw="", prefix="bridge", topic=Topic.MAILBOX, segments=("read",))
    await handle_mb(route_read, pb.CloudQueuedPublish(payload=b""))
    assert not mock_serial.send.called


@pytest.mark.asyncio
async def test_runtime_handle_file_and_shell_edge_branches(
    mock_bridge_service: BridgeService, mock_serial: AsyncMock, mocker: MockerFixture
) -> None:
    svc = mock_bridge_service
    handle_file: Callable[..., Awaitable[None]] = getattr(svc, "_handle_file")
    handle_shell: Callable[..., Awaitable[None]] = getattr(svc, "_handle_shell")
    handle_run_async: Callable[..., Awaitable[None]] = getattr(svc, "_handle_shell_run_async")

    svc.serial = None
    route_file = TopicRoute(raw="", prefix="bridge", topic=Topic.FILE, segments=("read", "test.txt"))
    await handle_file(route_file, pb.CloudQueuedPublish(payload=b""))
    assert not mock_serial.send.called

    svc.serial = mock_serial
    route_no_rem = TopicRoute(raw="", prefix="bridge", topic=Topic.FILE, segments=())
    await handle_file(route_no_rem, pb.CloudQueuedPublish(payload=b""))
    assert not mock_serial.send.called

    route_unsafe = TopicRoute(raw="", prefix="bridge", topic=Topic.FILE, segments=("read", "../../../etc/shadow"))
    mocker.patch.object(svc, "_get_safe_path", return_value=None)
    await handle_file(route_unsafe, pb.CloudQueuedPublish(payload=b""))
    assert not mock_serial.send.called

    route_shell_empty = TopicRoute(raw="", prefix="bridge", topic=Topic.SHELL, segments=())
    await handle_shell(route_shell_empty, pb.CloudQueuedPublish(payload=b""))
    assert not mock_serial.send.called

    class Props:
        ContentType = "application/x-protobuf"

    class InboundWithProps:
        payload = pb.ProcessRunAsync(command="echo prop").SerializeToString()
        properties = Props()

    mock_rp = mocker.patch.object(svc, "run_process", new_callable=AsyncMock, return_value=123)
    await handle_run_async(0, cast(Any, InboundWithProps()))
    assert mock_rp.called

    mock_ctx = ProcessContext(AsyncMock())
    svc.state.running_processes[777] = mock_ctx
    mocker.patch.object(svc, "_terminate_process", side_effect=ProcessLookupError("No such process"))
    await svc.kill_process(777)
    assert 777 not in svc.state.running_processes


@pytest.mark.asyncio
async def test_runtime_handle_spi_and_pin_edge_branches(
    mock_bridge_service: BridgeService, mock_serial: AsyncMock
) -> None:
    svc = mock_bridge_service
    handle_spi: Callable[..., Awaitable[None]] = getattr(svc, "_handle_spi")
    handle_pin: Callable[..., Awaitable[None]] = getattr(svc, "_handle_pin")

    svc.serial = None
    route_spi = TopicRoute(raw="", prefix="bridge", topic=Topic.SPI, segments=("begin",))
    await handle_spi(route_spi, pb.CloudQueuedPublish(payload=b""))
    assert not mock_serial.send.called

    svc.serial = mock_serial
    route_spi_cfg = TopicRoute(raw="", prefix="bridge", topic=Topic.SPI, segments=("config",))
    await handle_spi(route_spi_cfg, pb.CloudQueuedPublish(payload=b"not-proto"))
    assert not mock_serial.send.called

    mock_serial.send.return_value = False
    route_spi_xfer = TopicRoute(raw="", prefix="bridge", topic=Topic.SPI, segments=("transfer",))
    await handle_spi(route_spi_xfer, pb.CloudQueuedPublish(payload=b"data"))
    assert mock_serial.send.called

    mock_serial.send.reset_mock()
    svc.serial = None
    route_pin = TopicRoute(raw="", prefix="bridge", topic=Topic.DIGITAL, segments=("13", "write"))
    await handle_pin(route_pin, pb.CloudQueuedPublish(payload=b"1"))
    assert not mock_serial.send.called

    svc.serial = mock_serial
    route_pin_invalid = TopicRoute(raw="", prefix="bridge", topic=Topic.DIGITAL, segments=("invalid_pin", "write"))
    await handle_pin(route_pin_invalid, pb.CloudQueuedPublish(payload=b"1"))
    assert not mock_serial.send.called

    route_pin_short = TopicRoute(raw="", prefix="bridge", topic=Topic.DIGITAL, segments=("13",))
    await handle_pin(route_pin_short, pb.CloudQueuedPublish(payload=b"1"))
    assert mock_serial.send.called


@pytest.mark.asyncio
async def test_runtime_run_cloud_cancelled(mock_bridge_service: BridgeService, mocker: MockerFixture) -> None:
    svc = mock_bridge_service
    svc.config.cloud_tls = False

    async def _mock_cloud_cancel(_tls: Any) -> None:
        raise asyncio.CancelledError()

    mocker.patch.object(svc, "connect_cloud_session", side_effect=_mock_cloud_cancel)
    with pytest.raises(asyncio.CancelledError):
        await svc.run_cloud()


@pytest.mark.asyncio
async def test_runtime_cloud_session_non_command_envelope(
    mock_bridge_service: BridgeService, mocker: MockerFixture
) -> None:
    svc = mock_bridge_service

    event_env = pb.CloudEnvelope(
        protocol_version=2,
        event=pb.EventNotification(event_type="custom", severity="info", description="test"),
    )

    class MockStream:
        def __init__(self, items: list[pb.CloudEnvelope]) -> None:
            self._items = items

        async def __aenter__(self) -> MockStream:
            return self

        async def __aexit__(self, exc_type: Any, exc_val: Any, exc_tb: Any) -> None:
            pass

        def __aiter__(self) -> MockStream:
            self._iter = iter(self._items)
            return self

        async def __anext__(self) -> pb.CloudEnvelope:
            try:
                return next(self._iter)
            except StopIteration:
                raise StopAsyncIteration

        async def send_message(self, _msg: Any) -> None:
            pass

    mock_stub = MagicMock()
    mock_stub.Session.open.return_value = MockStream([event_env])

    mocker.patch("mcubridge.services.runtime.Channel")
    mocker.patch("mcubridge.services.runtime.CloudBridgeStub", return_value=mock_stub)
    mocker.patch.object(svc, "flush_cloud_spool", new_callable=AsyncMock)
    mocker.patch.object(svc, "_send_cloud_event", new_callable=AsyncMock)
    await svc.connect_cloud_session(None)
    assert getattr(svc, "_cloud_stream") is None
    assert svc.state.cloud_fsm.spooling_degraded.is_active


@pytest.mark.asyncio
async def test_send_cloud_event_branches(mock_bridge_service: BridgeService) -> None:
    service = mock_bridge_service
    send_cloud_event: Callable[..., Awaitable[None]] = getattr(service, "_send_cloud_event")

    setattr(service, "_cloud_stream", None)
    await send_cloud_event("heartbeat", "info", "test_msg")

    mock_stream = AsyncMock()
    setattr(service, "_cloud_stream", mock_stream)
    await send_cloud_event("heartbeat", "info", "test_msg")
    mock_stream.send_message.assert_awaited_once()
    envelope = mock_stream.send_message.call_args[0][0]
    assert envelope.event.event_type == "heartbeat"
    assert envelope.event.description == "test_msg"


@pytest.mark.asyncio
async def test_runtime_on_mcu_datastore_get_branches(mock_bridge_service: BridgeService) -> None:
    svc = mock_bridge_service
    svc.serial = None
    req = pb.DatastoreGet(key="cfg/mode")
    assert await svc._on_mcu_datastore_get(1, req) is False

    mock_serial = AsyncMock()
    mock_serial.send = AsyncMock(return_value=True)
    svc.serial = mock_serial
    svc.state.datastore_cache = AsyncMock()
    svc.state.datastore_cache.get = AsyncMock(return_value=b"active")

    res = await svc._on_mcu_datastore_get(1, req)
    assert res is True
    mock_serial.send.assert_awaited_once()
    sent_cmd = mock_serial.send.call_args[0][0]
    sent_resp = mock_serial.send.call_args[0][1]
    assert sent_cmd == Command.CMD_DATASTORE_GET_RESP.value
    assert sent_resp.value == b"active"

    mock_serial.send.reset_mock()
    svc.state.datastore_cache = None
    res_none = await svc._on_mcu_datastore_get(2, req)
    assert res_none is True
    assert mock_serial.send.call_args[0][1].value == b""


@pytest.mark.asyncio
async def test_runtime_cloud_session_rpc_commands_and_errors(
    mock_bridge_service: BridgeService, mocker: MockerFixture
) -> None:
    svc = mock_bridge_service

    pong_env = pb.CloudEnvelope(protocol_version=2, pong=pb.KeepalivePong())
    cmd_ok = pb.CloudEnvelope(
        protocol_version=2,
        sequence_id=1,
        command_request=pb.CommandRequest(command_path="rpc/echo", payload=b"ping"),
    )
    cmd_invalid = pb.CloudEnvelope(
        protocol_version=2,
        sequence_id=2,
        command_request=pb.CommandRequest(command_path="rpc/bad", payload=b"err"),
    )
    cmd_os_err = pb.CloudEnvelope(
        protocol_version=2,
        sequence_id=3,
        command_request=pb.CommandRequest(command_path="rpc/crash", payload=b"fault"),
    )

    sent_envelopes: list[pb.CloudEnvelope] = []

    class MockRpcStream:
        def __init__(self) -> None:
            self._items = [pong_env, cmd_ok, cmd_invalid, cmd_os_err]
            self._idx = 0

        async def __aenter__(self) -> MockRpcStream:
            return self

        async def __aexit__(self, exc_type: Any, exc_val: Any, exc_tb: Any) -> None:
            pass

        def __aiter__(self) -> MockRpcStream:
            return self

        async def __anext__(self) -> pb.CloudEnvelope:
            if self._idx < len(self._items):
                item = self._items[self._idx]
                self._idx += 1
                return item
            raise ConnectionError("connection reset by peer")

        async def send_message(self, msg: pb.CloudEnvelope) -> None:
            sent_envelopes.append(msg)

    async def mock_execute_rpc(method: str, _payload: bytes) -> bytes:
        if method == "echo":
            return b"pong"
        if method == "bad":
            raise ValueError("Unknown RPC method")
        raise OSError("Hardware IO error")

    mocker.patch.object(svc.local_bridge_service, "execute_rpc", side_effect=mock_execute_rpc)

    mock_stub = MagicMock()
    mock_stub.Session.open.return_value = MockRpcStream()

    mocker.patch("mcubridge.services.runtime.Channel")
    mocker.patch("mcubridge.services.runtime.CloudBridgeStub", return_value=mock_stub)
    mocker.patch.object(svc, "flush_cloud_spool", new_callable=AsyncMock)
    mocker.patch.object(svc, "_send_cloud_event", new_callable=AsyncMock)

    mock_tls_cache = AsyncMock()
    mock_tls_cache.get = AsyncMock(return_value=b"cached_ticket")
    mock_tls_cache.set = AsyncMock()
    svc.state.tls_session_cache = mock_tls_cache

    with pytest.raises(ConnectionError, match="Cloud session stream disconnected"):
        await svc.connect_cloud_session(MagicMock())

    assert len(sent_envelopes) == 3
    assert sent_envelopes[0].command_response.status_code == 200
    assert sent_envelopes[0].command_response.payload == b"pong"
    assert sent_envelopes[1].command_response.status_code == 404
    assert "Unknown RPC method" in sent_envelopes[1].command_response.error_message
    assert sent_envelopes[2].command_response.status_code == 500
    assert "Hardware IO error" in sent_envelopes[2].command_response.error_message
    assert svc.state.cloud_fsm.spooling_degraded.is_active

