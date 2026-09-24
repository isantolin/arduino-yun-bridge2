"""Phase 3 SIL-2 Coverage Hardening Test Suite.

Targets comprehensive coverage across BridgeService runtime orchestration,
local file system transactions, MCU multiplexing, and serial communication.
"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from pathlib import Path
import tempfile
from typing import Any
from unittest.mock import AsyncMock, MagicMock

from pytest_mock import MockerFixture
import pytest

import mcubridge.protocol.mcubridge_pb2 as pb
from mcubridge.config.settings import RuntimeConfig
import mcubridge.config.const as const
import mcubridge.metrics as metrics_mod
from mcubridge.metrics import (
    publish_bridge_snapshots,
    publish_metrics,
)
from mcubridge.protocol.protocol import (
    PinAction,
    Status,
    Topic,
)
from mcubridge.protocol.structures import PendingPinRequest, TopicRoute
from mcubridge.services.runtime import BridgeService, LocalBridgeService, ProcessContext
from mcubridge.state.context import create_runtime_state
from mcubridge.state.storage import LmdbDeque
from mcubridge.transport.serial import SerialTransport


def _make_config(tmp_path: Path | None = None) -> RuntimeConfig:
    d = str(tmp_path or tempfile.mkdtemp())
    return RuntimeConfig(
        topic_prefix="test/br",
        serial_port="/dev/null",
        serial_baud=115200,
        cloud_spool_dir=d,
        cloud_queue_limit=10,
        allow_non_tmp_paths=True,
    )


def _make_service(config: RuntimeConfig) -> tuple[BridgeService, Any, AsyncMock]:
    state = create_runtime_state(config)
    mock_serial = AsyncMock(spec=SerialTransport)
    mock_serial.send = AsyncMock(return_value=True)
    mock_serial.send_raw = AsyncMock(return_value=True)
    mock_serial.acknowledge = AsyncMock()
    mock_serial.is_open = True
    service = BridgeService(config=config, state=state, serial=mock_serial)
    return service, state, mock_serial


# ══════════════════════════════════════════════════════════════════════════════
# 1. File Dispatch & Local File Operations
# ══════════════════════════════════════════════════════════════════════════════


@pytest.mark.asyncio
async def test_runtime_file_dispatch_handlers(tmp_path: Path) -> None:
    config = _make_config(tmp_path)
    service, _state, mock_serial = _make_service(config)

    test_file = tmp_path / "hello.txt"
    test_file.write_bytes(b"world")

    inbound = pb.CloudQueuedPublish(topic_name="test/br/file/read", payload=b"hello.txt")
    handle_file_fn: Callable[..., Awaitable[None]] = getattr(service, "_handle_file")

    # Local read/write/remove
    route_read = TopicRoute(raw="test/br/file/read", prefix=config.topic_prefix, topic=Topic.FILE, segments=("read",))
    await handle_file_fn(route_read, inbound)

    route_write = TopicRoute(
        raw="test/br/file/write", prefix=config.topic_prefix, topic=Topic.FILE, segments=("write",)
    )
    inbound_w = pb.CloudQueuedPublish(topic_name="test/br/file/write", payload=b"world")
    await handle_file_fn(route_write, inbound_w)

    route_remove = TopicRoute(
        raw="test/br/file/remove", prefix=config.topic_prefix, topic=Topic.FILE, segments=("remove",)
    )
    await handle_file_fn(route_remove, inbound)

    # MCU file write/remove
    route_mcu_write = TopicRoute(
        raw="test/br/file/write", prefix=config.topic_prefix, topic=Topic.FILE, segments=("write",)
    )
    inbound_mcu_w = pb.CloudQueuedPublish(topic_name="test/br/file/write", payload=b"mcu:test.txt")
    await handle_file_fn(route_mcu_write, inbound_mcu_w)

    route_mcu_remove = TopicRoute(
        raw="test/br/file/remove", prefix=config.topic_prefix, topic=Topic.FILE, segments=("remove",)
    )
    inbound_mcu_rm = pb.CloudQueuedPublish(topic_name="test/br/file/remove", payload=b"mcu:test.txt")
    await handle_file_fn(route_mcu_remove, inbound_mcu_rm)

    # MCU file read edge cases
    route_mcu_read = TopicRoute(
        raw="test/br/file/read", prefix=config.topic_prefix, topic=Topic.FILE, segments=("read",)
    )
    await handle_file_fn(route_mcu_read, inbound_mcu_rm)

    mock_serial.send_raw.return_value = False
    await handle_file_fn(route_mcu_read, inbound)

    service.cleanup()


@pytest.mark.asyncio
async def test_runtime_pin_handlers(tmp_path: Path) -> None:
    config = _make_config(tmp_path)
    service, _state, _mock_serial = _make_service(config)
    handle_pin_fn: Callable[..., Awaitable[None]] = getattr(service, "_handle_pin")

    pin_cases: tuple[tuple[Topic, tuple[str, ...], bytes], ...] = (
        (Topic.DIGITAL, ("13", PinAction.MODE.value), b"OUTPUT"),
        (Topic.DIGITAL, ("13",), b"1"),
        (Topic.DIGITAL, ("13", PinAction.READ.value), b""),
        (Topic.ANALOG, ("3",), b"128"),
        (Topic.ANALOG, ("1", PinAction.READ.value), b""),
    )
    for topic, segments, payload in pin_cases:
        path = "/".join(segments)
        topic_str = "d" if topic == Topic.DIGITAL else "a"
        raw_topic = f"test/br/{topic_str}/{path}"
        route = TopicRoute(raw=raw_topic, prefix=config.topic_prefix, topic=topic, segments=segments)
        inbound = pb.CloudQueuedPublish(topic_name=raw_topic, payload=payload)
        await handle_pin_fn(route, inbound)

    service.cleanup()


@pytest.mark.asyncio
async def test_runtime_spi_handlers(tmp_path: Path) -> None:
    config = _make_config(tmp_path)
    service, _state, _mock_serial = _make_service(config)
    handle_spi_fn: Callable[..., Awaitable[None]] = getattr(service, "_handle_spi")

    cfg_bytes = pb.SpiConfig(frequency=1000000, bit_order=1, data_mode=0).SerializeToString()
    spi_cases: tuple[tuple[str, bytes], ...] = (
        ("begin", b""),
        ("end", b""),
        ("config", cfg_bytes),
        ("transfer", b"ping"),
    )
    for action, payload in spi_cases:
        raw_topic = f"test/br/spi/{action}"
        route = TopicRoute(raw=raw_topic, prefix=config.topic_prefix, topic=Topic.SPI, segments=(action,))
        inbound = pb.CloudQueuedPublish(topic_name=raw_topic, payload=payload)
        await handle_spi_fn(route, inbound)

    service.cleanup()


@pytest.mark.asyncio
async def test_runtime_system_handlers(tmp_path: Path) -> None:
    config = _make_config(tmp_path)
    service, _state, _mock_serial = _make_service(config)
    handle_system_fn: Callable[..., Awaitable[None]] = getattr(service, "_handle_system")

    actions = ("bootloader", "reset", "ping", "sync", "handshake")
    for action in actions:
        raw_topic = f"test/br/system/{action}"
        route = TopicRoute(raw=raw_topic, prefix=config.topic_prefix, topic=Topic.SYSTEM, segments=(action,))
        inbound = pb.CloudQueuedPublish(topic_name=raw_topic, payload=b"")
        await handle_system_fn(route, inbound)

    service.cleanup()


@pytest.mark.asyncio
async def test_runtime_cloud_spool_operations(tmp_path: Path) -> None:
    config = _make_config(tmp_path)
    service, _state, _ = _make_service(config)

    # Initialize spool
    spool_dir = tmp_path / "spool"
    spool_dir.mkdir(parents=True, exist_ok=True)
    spool = LmdbDeque(path=str(spool_dir), maxlen=5)
    setattr(service, "_cloud_spool", spool)
    spool_msg_fn: Callable[..., Awaitable[bool]] = getattr(service, "_spool_cloud_message_locked")
    flush_spool_fn: Callable[[], Awaitable[None]] = getattr(service, "_flush_cloud_spool_locked")

    msg = pb.CloudQueuedPublish(topic_name="test/br/msg", payload=b"payload")
    res = await spool_msg_fn(msg)
    assert res is True
    assert len(spool) == 1

    # Flush when stream is None (noop)
    setattr(service, "_cloud_stream", None)
    await flush_spool_fn()
    assert len(spool) == 1

    # Flush with active stream
    mock_stream = AsyncMock()
    setattr(service, "_cloud_stream", mock_stream)
    await flush_spool_fn()
    assert len(spool) == 0
    assert mock_stream.send_message.called

    await spool.close()
    service.cleanup()


@pytest.mark.asyncio
async def test_runtime_supervisor_lifecycle(tmp_path: Path) -> None:
    config = _make_config(tmp_path)
    service, _state, _ = _make_service(config)

    # 1. Normal execution
    execution_count = 0

    async def normal_task() -> None:
        nonlocal execution_count
        execution_count += 1

    await service.supervise("normal", normal_task)
    assert execution_count == 1

    # 2. Fatal / Retry exhaustion error handling
    async def failing_task() -> None:
        raise RuntimeError("simulated fatal error")

    with pytest.raises(RuntimeError):
        await service.supervise("failing", failing_task, max_restarts=1, min_backoff=0.001, max_backoff=0.001, jitter=0)

    # 3. Cancellation handling
    async def cancelled_task() -> None:
        await asyncio.sleep(10)

    task_coro = asyncio.create_task(service.supervise("cancelled", cancelled_task))
    await asyncio.sleep(0.01)
    task_coro.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task_coro

    service.cleanup()


@pytest.mark.asyncio
async def test_local_bridge_grpc_service(tmp_path: Path) -> None:
    config = _make_config(tmp_path)
    service, _state, _ = _make_service(config)
    local_service = LocalBridgeService(service)

    # 1. Publish with response
    mock_stream = AsyncMock()
    req_pub = pb.CloudQueuedPublish(
        topic_name="test/br/d/13/read",
        payload=b"",
        correlation_data=b"12345678",
    )
    mock_stream.recv_message.return_value = req_pub

    async def reply_cor() -> None:
        await asyncio.sleep(0.01)
        if b"12345678" in service.ipc_requests:
            q = service.ipc_requests[b"12345678"]
            await q.put(pb.CloudQueuedPublish(topic_name="test/br/d/13/read/res", payload=b"1"))

    asyncio.create_task(reply_cor())
    await local_service.Publish(mock_stream)
    assert mock_stream.send_message.called

    # 2. Publish timeout
    mock_stream.reset_mock()
    req_pub_timeout = pb.CloudQueuedPublish(
        topic_name="test/br/d/13/read",
        payload=b"",
        correlation_data=b"timeout_cor",
    )
    mock_stream.recv_message.return_value = req_pub_timeout
    with pytest.raises(TimeoutError):
        async with asyncio.timeout(0.05):
            await local_service.Publish(mock_stream)

    # 3. Autoreply without correlation
    mock_stream.reset_mock()
    req_pub_autoreply = pb.CloudQueuedPublish(
        topic_name="test/br/system/ping",
        payload=b"",
        correlation_data=b"",
    )
    mock_stream.recv_message.return_value = req_pub_autoreply

    async def reply_auto_cor() -> None:
        await asyncio.sleep(0.01)
        # Service processes and directly returns

    asyncio.create_task(reply_auto_cor())
    await local_service.Publish(mock_stream)
    assert mock_stream.send_message.called

    service.cleanup()


@pytest.mark.asyncio
async def test_runtime_unsupported_mcu_request(tmp_path: Path) -> None:
    config = _make_config(tmp_path)
    service, _state, mock_serial = _make_service(config)

    unsupported_fn: Callable[..., Awaitable[bool]] = getattr(service, "_unsupported_mcu_request")
    res = await unsupported_fn(1, None, "unsupported_test")
    assert res is True
    assert mock_serial.send.called

    # When serial is None
    service.serial = None
    mock_serial.reset_mock()
    res = await unsupported_fn(1, None, "unsupported_test")
    assert res is False

    service.cleanup()


@pytest.mark.asyncio
async def test_runtime_on_mcu_analog_read_resp(tmp_path: Path) -> None:
    config = _make_config(tmp_path)
    service, state, _ = _make_service(config)

    state.pending_analog_reads.append(PendingPinRequest(pin=0, reply_context=None))
    resp = pb.AnalogReadResponse(value=512)
    on_ana_fn: Callable[..., Awaitable[None]] = getattr(service, "_on_mcu_analog_read_resp")
    await on_ana_fn(1, resp)

    assert len(state.pending_analog_reads) == 0

    service.cleanup()


@pytest.mark.asyncio
async def test_runtime_on_mcu_process_kill(tmp_path: Path) -> None:
    config = _make_config(tmp_path)
    service, state, _ = _make_service(config)

    mock_proc = MagicMock()
    mock_proc.pid = 1234
    ctx = ProcessContext(handle=mock_proc)
    state.running_processes[1] = ctx

    kill_req = pb.ProcessKill(pid=1)
    on_kill_fn: Callable[..., Awaitable[None]] = getattr(service, "_on_mcu_process_kill")
    await on_kill_fn(1, kill_req)

    assert 1 not in state.running_processes

    service.cleanup()


@pytest.mark.asyncio
async def test_runtime_handle_mcu_status_payloads(tmp_path: Path, mocker: MockerFixture) -> None:
    config = _make_config(tmp_path)
    service, _state, _ = _make_service(config)

    mock_enqueue = mocker.patch.object(service, "enqueue_cloud", new_callable=AsyncMock)
    handle_status_fn: Callable[..., Awaitable[None]] = getattr(service, "_handle_mcu_status")
    # 1. ProtobufMessage payload
    msg = pb.GenericResponse(message="test_msg")
    await handle_status_fn(Status.ERROR, 1, msg)
    assert mock_enqueue.call_count == 1

    # 2. Raw bytes payload with valid Protobuf
    b_msg = pb.GenericResponse(message="bytes_msg").SerializeToString()
    await handle_status_fn(Status.TIMEOUT, 2, b_msg)
    assert mock_enqueue.call_count == 2

    # 3. Corrupted raw bytes
    await handle_status_fn(Status.MALFORMED, 3, b"\xff\xff\xff")
    assert mock_enqueue.call_count == 3

    service.cleanup()


@pytest.mark.asyncio
async def test_runtime_enqueue_cloud_drop(tmp_path: Path, mocker: MockerFixture) -> None:
    config = _make_config(tmp_path)
    config.cloud_enabled = True
    service, state, _ = _make_service(config)

    state.cloud_queue_limit = 1
    mocker.patch.object(service, "_spool_cloud_message_locked", new_callable=AsyncMock, return_value=False)
    # Trigger drop
    await service.enqueue_cloud(pb.CloudQueuedPublish(topic_name="test2", payload=b"2"))

    assert state.cloud_dropped_messages > 0

    service.cleanup()


@pytest.mark.asyncio
async def test_runtime_console_queues_distribution(tmp_path: Path) -> None:
    config = _make_config(tmp_path)
    service, _state, _ = _make_service(config)

    q1: asyncio.Queue[pb.CloudQueuedPublish] = asyncio.Queue()
    service.console_queues.append(q1)

    console_msg = pb.ConsoleWrite(data=b"console_output")
    on_console_fn: Callable[..., Awaitable[None]] = getattr(service, "_on_mcu_console_write")
    await on_console_fn(1, console_msg)

    assert not q1.empty()
    received = await q1.get()
    assert received.payload == b"console_output"

    service.console_queues.remove(q1)
    service.cleanup()


# ══════════════════════════════════════════════════════════════════════════════
# 2. Handshake Service: Edge Cases & Error Branches
# ══════════════════════════════════════════════════════════════════════════════


@pytest.mark.asyncio
async def test_handshake_handle_capabilities_resp(tmp_path: Path) -> None:
    config = _make_config(tmp_path)
    service, state, _ = _make_service(config)
    handshake = service.handshake

    cap_proto = pb.Capabilities(
        watchdog=True,
    )
    parse_cap_fn: Callable[[pb.Capabilities], None] = getattr(handshake, "_parse_capabilities")
    parse_cap_fn(cap_proto)
    assert isinstance(state.mcu_capabilities, pb.Capabilities) and state.mcu_capabilities.watchdog is True

    fut: asyncio.Future[Any] = asyncio.get_running_loop().create_future()
    setattr(handshake, "_capabilities_future", fut)
    await handshake.handle_capabilities_resp(1, cap_proto)
    assert fut.done()
    assert fut.result() == cap_proto

    service.cleanup()


@pytest.mark.asyncio
async def test_handshake_handle_link_sync_resp(tmp_path: Path) -> None:
    config = _make_config(tmp_path)
    service, state, _ = _make_service(config)
    handshake = service.handshake

    # 1. Nonce mismatch
    state.link_handshake_nonce = b"expected_nonce"
    bad_nonce = pb.LinkSync(nonce=b"bad_nonce", tag=b"tag")
    res = await handshake.handle_link_sync_resp(1, bad_nonce)
    assert res is False

    # 2. Tag mismatch
    good_nonce_bad_tag = pb.LinkSync(nonce=b"expected_nonce", tag=b"bad_tag")
    res = await handshake.handle_link_sync_resp(1, good_nonce_bad_tag)
    assert res is False

    # 3. Successful sync
    handshake.fsm.reset()
    handshake.fsm.start_sync()
    handshake.fsm.reset_sent()
    state.link_handshake_nonce = b"expected_nonce"
    valid_tag = handshake.calculate_handshake_tag(config.serial_shared_secret, b"expected_nonce")
    state.link_expected_tag = valid_tag
    good_sync = pb.LinkSync(nonce=b"expected_nonce", tag=valid_tag)
    res = await handshake.handle_link_sync_resp(1, good_sync)
    assert res is True
    assert state.is_synchronized

    service.cleanup()


@pytest.mark.asyncio
async def test_handshake_handle_link_reset_resp(tmp_path: Path) -> None:
    config = _make_config(tmp_path)
    service, _state, _ = _make_service(config)
    handshake = service.handshake

    res = await handshake.handle_link_reset_resp(1, pb.GenericResponse(message="reset_ok"))
    assert res is True

    service.cleanup()


# ══════════════════════════════════════════════════════════════════════════════
# 3. Metrics Collector & Telemetry Push
# ══════════════════════════════════════════════════════════════════════════════


def test_build_metrics_message_with_extra_props(tmp_path: Path) -> None:
    config = _make_config(tmp_path)
    state = create_runtime_state(config)
    state.file_storage_limit_rejections = 1

    metrics_snap = state.build_metrics_snapshot()
    metrics_snap.cloud_spool_degraded = True
    metrics_snap.cloud_spool_failure_reason = "disk full"
    build_msg_fn: Callable[..., pb.CloudQueuedPublish] = getattr(metrics_mod, "_build_metrics_message")
    msg = build_msg_fn(state, metrics_snap, expiry_seconds=30.0)

    assert msg.topic_name == f"{config.topic_prefix}/system/metrics"
    assert len(msg.payload) > 0
    user_props = {p.key: p.value for p in msg.user_properties}
    assert user_props.get(const.PROP_KEY_BRIDGE_SPOOL) == "disk full"
    assert user_props.get(const.PROP_KEY_BRIDGE_FILES) == const.PROP_VAL_QUOTA_BLOCKED
    assert user_props.get(const.PROP_KEY_WATCHDOG_ENABLED) == const.PROP_VAL_ENABLED_FALSE

    state.cleanup()


@pytest.mark.asyncio
async def test_emit_bridge_snapshot_flavors(tmp_path: Path) -> None:
    config = _make_config(tmp_path)
    state = create_runtime_state(config)

    mock_enqueue = AsyncMock()
    emit_snap_fn: Callable[..., Awaitable[None]] = getattr(metrics_mod, "_emit_bridge_snapshot")

    # 1. Summary
    await emit_snap_fn(state, mock_enqueue, flavor="summary")
    assert mock_enqueue.called

    # 2. Handshake
    mock_enqueue.reset_mock()
    await emit_snap_fn(state, mock_enqueue, flavor="handshake")
    assert mock_enqueue.called

    # 3. Invalid flavor (fallback to bridge snapshot)
    mock_enqueue.reset_mock()
    await emit_snap_fn(state, mock_enqueue, flavor="unknown")
    assert mock_enqueue.called

    state.cleanup()


@pytest.mark.asyncio
async def test_publish_metrics_lifecycle(tmp_path: Path) -> None:
    config = _make_config(tmp_path)
    state = create_runtime_state(config)

    mock_enqueue = AsyncMock()
    task = asyncio.create_task(publish_metrics(state, mock_enqueue, interval=0.01, min_interval=0.01))
    await asyncio.sleep(0.05)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    assert mock_enqueue.called
    state.cleanup()


@pytest.mark.asyncio
async def test_publish_metrics_failing_enqueue(tmp_path: Path) -> None:
    config = _make_config(tmp_path)
    state = create_runtime_state(config)

    call_count = 0

    async def failing_enqueue(msg: pb.CloudQueuedPublish) -> None:
        nonlocal call_count
        call_count += 1
        raise RuntimeError("enqueue failed")

    task = asyncio.create_task(publish_metrics(state, failing_enqueue, interval=0.01, min_interval=0.01))
    await asyncio.sleep(0.05)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    assert call_count > 0
    state.cleanup()


@pytest.mark.asyncio
async def test_publish_bridge_snapshots_lifecycle(tmp_path: Path) -> None:
    config = _make_config(tmp_path)
    state = create_runtime_state(config)

    mock_enqueue = AsyncMock()
    task = asyncio.create_task(
        publish_bridge_snapshots(state, mock_enqueue, summary_interval=0.01, handshake_interval=0.01, min_interval=0.01)
    )
    await asyncio.sleep(0.05)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    assert mock_enqueue.called
    state.cleanup()


@pytest.mark.asyncio
async def test_publish_bridge_snapshots_failing_enqueue(tmp_path: Path) -> None:
    config = _make_config(tmp_path)
    state = create_runtime_state(config)

    call_count = 0

    async def failing_enqueue(msg: pb.CloudQueuedPublish) -> None:
        nonlocal call_count
        call_count += 1
        raise RuntimeError("enqueue failed")

    task = asyncio.create_task(
        publish_bridge_snapshots(
            state, failing_enqueue, summary_interval=0.01, handshake_interval=0.01, min_interval=0.01
        )
    )
    await asyncio.sleep(0.05)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    assert call_count > 0
    state.cleanup()


# ══════════════════════════════════════════════════════════════════════════════
# 4. Storage & LMDB Deque Operations
# ══════════════════════════════════════════════════════════════════════════════


@pytest.mark.asyncio
async def test_lmdb_deque_operations(tmp_path: Path) -> None:
    deque_path = str(tmp_path / "test_deque")
    deque = LmdbDeque(path=deque_path, maxlen=5)

    # 1. Append and popleft
    await deque.append(b"item1")
    await deque.append(b"item2")
    assert len(deque) == 2

    item = await deque.popleft()
    assert item == b"item1"
    assert len(deque) == 1

    # 2. Peek
    peeked = await deque.peek()
    assert peeked == b"item2"
    assert len(deque) == 1

    # 3. Pop remaining
    item2 = await deque.popleft()
    assert item2 == b"item2"
    assert len(deque) == 0

    # 4. Pop empty raises IndexError
    with pytest.raises(IndexError):
        await deque.popleft()

    # 5. Peek empty raises IndexError
    with pytest.raises(IndexError):
        await deque.peek()

    # 6. Overflow drops oldest
    for i in range(10):
        await deque.append(f"overflow_{i}".encode())
    assert len(deque) == 5
    oldest = await deque.popleft()
    assert oldest == b"overflow_5"

    await deque.close()


# ══════════════════════════════════════════════════════════════════════════════
# 6. Service Lifecycle, Telemetry Envelopes & MCU Frame Dispatch
# ══════════════════════════════════════════════════════════════════════════════


@pytest.mark.asyncio
async def test_service_serial_lifecycle(tmp_path: Path, mocker: MockerFixture) -> None:
    config = _make_config(tmp_path)
    service, state, mock_serial = _make_service(config)

    # 1. On serial connected (synchronized)
    async def mock_sync_impl() -> bool:
        state.connection_fsm.synchronize()
        return True

    mock_sync = mocker.patch.object(service.handshake, "synchronize", side_effect=mock_sync_impl)
    await service.on_serial_connected()
    assert mock_sync.called
    assert mock_serial.send.called

    # 2. On serial disconnected
    state.pending_digital_reads.append(PendingPinRequest(pin=2, reply_context=None))
    await service.on_serial_disconnected()
    assert len(state.pending_digital_reads) == 0
    assert not state.is_synchronized
    assert mock_serial.reset.called

    # 3. Cleanup socket unlinking exception
    mocker.patch("pathlib.Path.unlink", side_effect=OSError("unlink error"))
    service.cleanup()


@pytest.mark.asyncio
async def test_service_handle_mcu_frame_dispatch(tmp_path: Path) -> None:
    config = _make_config(tmp_path)
    service, state, mock_serial = _make_service(config)

    state.connection_fsm.synchronize()

    # 1. Registered MCU command
    msg = pb.ConsoleWrite(data=b"hello mcu")
    on_console_fn: Callable[..., Awaitable[None]] = getattr(service, "_on_mcu_console_write")
    await on_console_fn(1, msg)
    assert len(service.console_queues) == 0

    # 2. Digital Read Response
    state.pending_digital_reads.append(PendingPinRequest(pin=13, reply_context=None))
    resp = pb.DigitalReadResponse(value=1)
    on_d_fn: Callable[..., Awaitable[None]] = getattr(service, "_on_mcu_digital_read_resp")
    await on_d_fn(2, resp)
    assert len(state.pending_digital_reads) == 0

    # 3. Analog Read Response
    state.pending_analog_reads.append(PendingPinRequest(pin=0, reply_context=None))
    ana_resp = pb.AnalogReadResponse(value=512)
    on_a_fn: Callable[..., Awaitable[None]] = getattr(service, "_on_mcu_analog_read_resp")
    await on_a_fn(3, ana_resp)
    assert len(state.pending_analog_reads) == 0

    # 4. XON / XOFF flow control
    on_xoff_fn: Callable[..., Awaitable[None]] = getattr(service, "_handle_mcu_xoff")
    await on_xoff_fn(4, b"")
    assert state.mcu_is_paused
    assert not state.serial_tx_allowed.is_set()

    on_xon_fn: Callable[..., Awaitable[None]] = getattr(service, "_handle_mcu_xon")
    await on_xon_fn(5, b"")
    assert not state.mcu_is_paused
    assert state.serial_tx_allowed.is_set()

    # 5. Datastore put / get
    on_ds_put_fn: Callable[..., Awaitable[bool]] = getattr(service, "_on_mcu_datastore_put")
    on_ds_get_fn: Callable[..., Awaitable[bool]] = getattr(service, "_on_mcu_datastore_get")
    await on_ds_put_fn(6, pb.DatastorePut(key="k1", value=b"v1"))
    await on_ds_get_fn(7, pb.DatastoreGet(key="k1"))
    assert mock_serial.send.called

    service.cleanup()


@pytest.mark.asyncio
async def test_service_publish_cloud_message_flavors(tmp_path: Path) -> None:
    config = _make_config(tmp_path)
    service, _state, _ = _make_service(config)

    mock_stream = AsyncMock()
    setattr(service, "_cloud_stream", mock_stream)
    pub_cloud_fn: Callable[..., Awaitable[bool]] = getattr(service, "_publish_cloud_message")

    # 1. Direct publish
    msg = pb.CloudQueuedPublish(topic_name="test/topic", payload=b"payload")
    res = await pub_cloud_fn(msg)
    assert res is True
    assert mock_stream.send_message.called

    # 2. Stream None returns False
    setattr(service, "_cloud_stream", None)
    res_none = await pub_cloud_fn(msg)
    assert res_none is False

    # 3. Stream raises exception returns False
    setattr(service, "_cloud_stream", mock_stream)
    mock_stream.send_message.side_effect = OSError("send failed")
    res_err = await pub_cloud_fn(msg)
    assert res_err is False

    service.cleanup()


# ══════════════════════════════════════════════════════════════════════════════
# 7. Mailbox, Shell & Advanced MCU Handlers
# ══════════════════════════════════════════════════════════════════════════════


@pytest.mark.asyncio
async def test_runtime_mailbox_handlers(tmp_path: Path) -> None:
    config = _make_config(tmp_path)
    service, state, mock_serial = _make_service(config)

    # 1. Mailbox Write
    route_w = TopicRoute(
        raw="test/br/mailbox/write", prefix=config.topic_prefix, topic=Topic.MAILBOX, segments=("write",)
    )
    inbound_w = pb.CloudQueuedPublish(topic_name="test/br/mailbox/write", payload=b"msg1")
    handle_mb_fn: Callable[..., Awaitable[None]] = getattr(service, "_handle_mailbox")
    await handle_mb_fn(route_w, inbound_w)
    assert mock_serial.send.called
    assert len(state.mailbox_queue) == 1

    # 2. Mailbox Read (Empty)
    route_r = TopicRoute(
        raw="test/br/mailbox/read", prefix=config.topic_prefix, topic=Topic.MAILBOX, segments=("read",)
    )
    inbound_r = pb.CloudQueuedPublish(topic_name="test/br/mailbox/read", payload=b"")
    await handle_mb_fn(route_r, inbound_r)

    # 3. Mailbox Read (Non-empty)
    await state.mailbox_incoming_queue.append(b"incoming_data")
    await handle_mb_fn(route_r, inbound_r)

    service.cleanup()


@pytest.mark.asyncio
async def test_runtime_mcu_file_read_and_timeouts(tmp_path: Path) -> None:
    config = _make_config(tmp_path)
    service, state, mock_serial = _make_service(config)

    inbound = pb.CloudQueuedPublish(topic_name="test/br/file/read", payload=b"mcu:test.txt")
    handle_mcu_read_fn: Callable[[str, pb.CloudQueuedPublish], Awaitable[None]] = getattr(
        service, "_handle_file_mcu_read"
    )

    # 1. Send failure
    mock_serial.send_raw.return_value = False
    await handle_mcu_read_fn("mcu:test.txt", inbound)
    assert mock_serial.send_raw.called

    # 2. Timeout waiting for response
    mock_serial.send_raw.return_value = True
    state.serial_response_timeout_ms = 10
    await handle_mcu_read_fn("mcu:test.txt", inbound)
    assert getattr(service, "_pending_mcu_read") is None

    service.cleanup()


@pytest.mark.asyncio
async def test_runtime_shell_dispatch_handlers(tmp_path: Path, mocker: MockerFixture) -> None:
    config = _make_config(tmp_path)
    service, state, _ = _make_service(config)

    # 1. Shell run async
    route_run = TopicRoute(
        raw="test/br/shell/run_async", prefix=config.topic_prefix, topic=Topic.SHELL, segments=("run_async",)
    )
    inbound_run = pb.CloudQueuedPublish(topic_name="test/br/shell/run_async", payload=b"echo hello")
    mock_run = mocker.patch.object(service, "run_process", new_callable=AsyncMock, return_value=123)
    handle_sh_fn: Callable[..., Awaitable[None]] = getattr(service, "_handle_shell")
    await handle_sh_fn(route_run, inbound_run)
    assert mock_run.called

    # 2. Shell run async with error
    mock_err_run = mocker.patch.object(service, "run_process", side_effect=OSError("spawn error"))
    await handle_sh_fn(route_run, inbound_run)
    assert mock_err_run.called

    # 3. Shell poll
    mock_proc = MagicMock()
    ctx = ProcessContext(handle=mock_proc)
    state.running_processes[123] = ctx

    route_poll = TopicRoute(
        raw="test/br/shell/poll/123", prefix=config.topic_prefix, topic=Topic.SHELL, segments=("poll", "123")
    )
    inbound_poll = pb.CloudQueuedPublish(topic_name="test/br/shell/poll/123", payload=b"")
    mock_poll = mocker.patch.object(service, "poll_process", new_callable=AsyncMock)
    mock_poll.return_value = pb.ProcessPollResponse(status=Status.OK.value, exit_code=0, finished=True)
    await handle_sh_fn(route_poll, inbound_poll)
    assert mock_poll.called

    # 4. Shell kill
    route_kill = TopicRoute(
        raw="test/br/shell/kill/123", prefix=config.topic_prefix, topic=Topic.SHELL, segments=("kill", "123")
    )
    inbound_kill = pb.CloudQueuedPublish(topic_name="test/br/shell/kill/123", payload=b"")
    mock_term = mocker.patch.object(service, "_terminate_process", new_callable=AsyncMock, return_value=0)
    await handle_sh_fn(route_kill, inbound_kill)
    assert mock_term.called

    service.cleanup()


@pytest.mark.asyncio
async def test_runtime_console_flush_and_queues(tmp_path: Path) -> None:
    config = _make_config(tmp_path)
    service, state, mock_serial = _make_service(config)

    state.connection_fsm.synchronize()

    flush_console_fn: Callable[[], Awaitable[None]] = getattr(service, "_flush_console_queue")

    # 1. Flush console queue
    state.console_to_mcu_queue.append(b"console_chunk")
    await flush_console_fn()
    assert mock_serial.send.called

    # 2. Flush console queue when serial send fails
    mock_serial.send.return_value = False
    state.console_to_mcu_queue.append(b"fail_chunk")
    await flush_console_fn()
    assert len(state.console_to_mcu_queue) > 0

    service.cleanup()


# ══════════════════════════════════════════════════════════════════════════════
# 8. Cloud Stream Session & Corrupt Spool Flush Hardening
# ══════════════════════════════════════════════════════════════════════════════


@pytest.mark.asyncio
async def test_runtime_cloud_session_stream_flow(tmp_path: Path, mocker: MockerFixture) -> None:
    config = _make_config(tmp_path)
    config.cloud_http3_enabled = True
    service, state, _ = _make_service(config)

    envelope_pong = pb.CloudEnvelope(
        protocol_version=2,
        device_id=state.device_id,
        sequence_id=1,
        pong=pb.KeepalivePong(roundtrip_ms=10),
    )
    envelope_cmd = pb.CloudEnvelope(
        protocol_version=2,
        device_id=state.device_id,
        sequence_id=2,
        command_request=pb.CommandRequest(
            command_path="rpc/SetPinMode",
            payload=pb.PinMode(pin=13, mode=pb.PIN_OUTPUT).SerializeToString(),
        ),
    )

    mock_stream = None

    class MockAsyncStream:
        def __init__(self) -> None:
            self._messages = [envelope_pong, envelope_cmd]
            self.send_message = AsyncMock()

        def __aiter__(self) -> MockAsyncStream:
            return self

        async def __anext__(self) -> pb.CloudEnvelope:
            if self._messages:
                return self._messages.pop(0)
            raise StopAsyncIteration

    class MockSessionContext:
        async def __aenter__(self) -> MockAsyncStream:
            nonlocal mock_stream
            mock_stream = MockAsyncStream()
            return mock_stream

        async def __aexit__(self, *args: Any) -> None:
            pass

    mocker.patch("mcubridge.services.runtime.Channel")
    mock_stub_cls = mocker.patch("mcubridge.services.runtime.CloudBridgeStub")

    mock_stub = MagicMock()
    mock_stub.Session.open.return_value = MockSessionContext()
    mock_stub_cls.return_value = mock_stub

    await service.connect_cloud_session(None)
    assert state.connected_via_http3
    assert mock_stream is not None
    assert mock_stream.send_message.await_count == 2
    resp_env = mock_stream.send_message.call_args_list[1][0][0]
    assert resp_env.sequence_id == 2
    assert resp_env.command_response.status_code == 200

    service.cleanup()


@pytest.mark.asyncio
async def test_runtime_flush_cloud_spool_corrupt_and_errors(tmp_path: Path) -> None:
    config = _make_config(tmp_path)
    service, state, _ = _make_service(config)

    mock_spool = MagicMock(spec=LmdbDeque)
    mock_spool.__len__.side_effect = [2, 1, 0, 0]
    mock_spool.peek = AsyncMock(side_effect=[b"\xff\xffinvalid_protobuf", b""])
    mock_spool.popleft = AsyncMock()

    setattr(service, "_cloud_spool", mock_spool)
    setattr(service, "_cloud_stream", AsyncMock())
    flush_spool_fn: Callable[[], Awaitable[None]] = getattr(service, "_flush_cloud_spool_locked")

    await flush_spool_fn()
    assert state.cloud_spool_corrupt_dropped > 0

    service.cleanup()
