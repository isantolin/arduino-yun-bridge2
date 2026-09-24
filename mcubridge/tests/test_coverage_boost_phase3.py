# pyright: reportPrivateUsage=false
"""Phase 3 SIL-2 Coverage Hardening Test Suite.

Targets comprehensive coverage across BridgeService runtime orchestration,
local file system transactions, MCU multiplexing, and serial communication.
"""

from __future__ import annotations

import asyncio
from pathlib import Path
import tempfile
from typing import Any
from unittest.mock import AsyncMock, MagicMock

from pytest_mock import MockerFixture
import pytest

import mcubridge.protocol.mcubridge_pb2 as pb
from mcubridge.config.settings import RuntimeConfig
from mcubridge.metrics import (
    _build_metrics_message,
    _emit_bridge_snapshot,
    publish_bridge_snapshots,
    publish_metrics,
)
from mcubridge.protocol.protocol import (
    Command,
    DatastoreAction,
    FileAction,
    PinAction,
    ShellAction,
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
    service, state, mock_serial = _make_service(config)

    test_file = tmp_path / "hello.txt"
    test_file.write_bytes(b"world")

    inbound = pb.CloudQueuedPublish(topic_name="test/br/file/read", payload=b"hello.txt")

    # Local read/write/remove
    route_read = TopicRoute(
        raw="test/br/file/read", prefix=config.topic_prefix, topic=Topic.FILE, segments=("read",)
    )
    await service._handle_file(route_read, inbound)

    route_write = TopicRoute(
        raw="test/br/file/write", prefix=config.topic_prefix, topic=Topic.FILE, segments=("write",)
    )
    inbound_w = pb.CloudQueuedPublish(topic_name="test/br/file/write", payload=b"world")
    await service._handle_file(route_write, inbound_w)

    route_remove = TopicRoute(
        raw="test/br/file/remove", prefix=config.topic_prefix, topic=Topic.FILE, segments=("remove",)
    )
    await service._handle_file(route_remove, inbound)

    # MCU file write/remove
    route_mcu_write = TopicRoute(
        raw="test/br/file/write", prefix=config.topic_prefix, topic=Topic.FILE, segments=("write",)
    )
    inbound_mcu_w = pb.CloudQueuedPublish(topic_name="test/br/file/write", payload=b"mcu:test.txt")
    await service._handle_file(route_mcu_write, inbound_mcu_w)

    route_mcu_remove = TopicRoute(
        raw="test/br/file/remove", prefix=config.topic_prefix, topic=Topic.FILE, segments=("remove",)
    )
    inbound_mcu_rm = pb.CloudQueuedPublish(topic_name="test/br/file/remove", payload=b"mcu:test.txt")
    await service._handle_file(route_mcu_remove, inbound_mcu_rm)

    # MCU file read edge cases
    route_mcu_read = TopicRoute(
        raw="test/br/file/read", prefix=config.topic_prefix, topic=Topic.FILE, segments=("read",)
    )
    await service._handle_file(route_mcu_read, inbound_mcu_rm)

    mock_serial.send_raw.return_value = False
    await service._handle_file(route_mcu_read, inbound)

    service.cleanup()


@pytest.mark.asyncio
async def test_runtime_pin_handlers(tmp_path: Path) -> None:
    config = _make_config(tmp_path)
    service, state, mock_serial = _make_service(config)

    # 1. Digital Mode
    route_mode = TopicRoute(
        raw="test/br/d/13/mode", prefix=config.topic_prefix, topic=Topic.DIGITAL, segments=("13", PinAction.MODE.value)
    )
    inbound_mode = pb.CloudQueuedPublish(topic_name="test/br/d/13/mode", payload=b"OUTPUT")
    await service._handle_pin(route_mode, inbound_mode)

    # 2. Digital Write
    route_write = TopicRoute(
        raw="test/br/d/13", prefix=config.topic_prefix, topic=Topic.DIGITAL, segments=("13",)
    )
    inbound_write = pb.CloudQueuedPublish(topic_name="test/br/d/13", payload=b"1")
    await service._handle_pin(route_write, inbound_write)

    # 3. Digital Read
    route_read = TopicRoute(
        raw="test/br/d/13/read", prefix=config.topic_prefix, topic=Topic.DIGITAL, segments=("13", PinAction.READ.value)
    )
    inbound_read = pb.CloudQueuedPublish(topic_name="test/br/d/13/read", payload=b"")
    await service._handle_pin(route_read, inbound_read)

    # 4. Analog Write
    route_ana_write = TopicRoute(
        raw="test/br/a/3", prefix=config.topic_prefix, topic=Topic.ANALOG, segments=("3",)
    )
    inbound_ana_write = pb.CloudQueuedPublish(topic_name="test/br/a/3", payload=b"128")
    await service._handle_pin(route_ana_write, inbound_ana_write)

    # 5. Analog Read
    route_ana_read = TopicRoute(
        raw="test/br/a/1/read", prefix=config.topic_prefix, topic=Topic.ANALOG, segments=("1", PinAction.READ.value)
    )
    inbound_ana_read = pb.CloudQueuedPublish(topic_name="test/br/a/1/read", payload=b"")
    await service._handle_pin(route_ana_read, inbound_ana_read)

    service.cleanup()


@pytest.mark.asyncio
async def test_runtime_spi_handlers(tmp_path: Path) -> None:
    config = _make_config(tmp_path)
    service, _state, mock_serial = _make_service(config)

    # 1. SPI Begin
    route_begin = TopicRoute(raw="test/br/spi/begin", prefix=config.topic_prefix, topic=Topic.SPI, segments=("begin",))
    inbound_empty = pb.CloudQueuedPublish(topic_name="test/br/spi/begin", payload=b"")
    await service._handle_spi(route_begin, inbound_empty)

    # 2. SPI End
    route_end = TopicRoute(raw="test/br/spi/end", prefix=config.topic_prefix, topic=Topic.SPI, segments=("end",))
    await service._handle_spi(route_end, inbound_empty)

    # 3. SPI Config
    route_cfg = TopicRoute(raw="test/br/spi/config", prefix=config.topic_prefix, topic=Topic.SPI, segments=("config",))
    cfg_proto = pb.SpiConfig(frequency=1000000, bit_order=1, data_mode=0)
    inbound_cfg = pb.CloudQueuedPublish(topic_name="test/br/spi/config", payload=cfg_proto.SerializeToString())
    await service._handle_spi(route_cfg, inbound_cfg)

    # 4. SPI Transfer
    route_xfer = TopicRoute(
        raw="test/br/spi/transfer", prefix=config.topic_prefix, topic=Topic.SPI, segments=("transfer",)
    )
    inbound_xfer = pb.CloudQueuedPublish(topic_name="test/br/spi/transfer", payload=b"ping")
    await service._handle_spi(route_xfer, inbound_xfer)

    service.cleanup()


@pytest.mark.asyncio
async def test_runtime_system_handlers(tmp_path: Path) -> None:
    config = _make_config(tmp_path)
    service, _state, mock_serial = _make_service(config)

    # 1. System Bootloader
    route_boot = TopicRoute(
        raw="test/br/system/bootloader", prefix=config.topic_prefix, topic=Topic.SYSTEM, segments=("bootloader",)
    )
    inbound = pb.CloudQueuedPublish(topic_name="test/br/system/bootloader", payload=b"")
    await service._handle_system(route_boot, inbound)

    # 2. System Reset
    route_rst = TopicRoute(
        raw="test/br/system/reset", prefix=config.topic_prefix, topic=Topic.SYSTEM, segments=("reset",)
    )
    await service._handle_system(route_rst, inbound)

    # 3. System Ping
    route_ping = TopicRoute(
        raw="test/br/system/ping", prefix=config.topic_prefix, topic=Topic.SYSTEM, segments=("ping",)
    )
    await service._handle_system(route_ping, inbound)

    # 4. System Sync
    route_sync = TopicRoute(
        raw="test/br/system/sync", prefix=config.topic_prefix, topic=Topic.SYSTEM, segments=("sync",)
    )
    await service._handle_system(route_sync, inbound)

    # 5. System Handshake
    route_hs = TopicRoute(
        raw="test/br/system/handshake", prefix=config.topic_prefix, topic=Topic.SYSTEM, segments=("handshake",)
    )
    await service._handle_system(route_hs, inbound)

    service.cleanup()


@pytest.mark.asyncio
async def test_runtime_cloud_spool_operations(tmp_path: Path) -> None:
    config = _make_config(tmp_path)
    service, state, _ = _make_service(config)

    # Initialize spool
    spool_dir = tmp_path / "spool"
    spool_dir.mkdir(parents=True, exist_ok=True)
    service._cloud_spool = LmdbDeque(path=str(spool_dir), maxlen=5)

    msg = pb.CloudQueuedPublish(topic_name="test/br/msg", payload=b"payload")
    res = await service._spool_cloud_message_locked(msg)
    assert res is True
    assert len(service._cloud_spool) == 1

    # Flush when stream is None (noop)
    service._cloud_stream = None
    await service._flush_cloud_spool_locked()
    assert len(service._cloud_spool) == 1

    # Flush with active stream
    mock_stream = AsyncMock()
    service._cloud_stream = mock_stream
    await service._flush_cloud_spool_locked()
    assert len(service._cloud_spool) == 0
    assert mock_stream.send_message.called

    if service._cloud_spool:
        await service._cloud_spool.close()
    service.cleanup()


@pytest.mark.asyncio
async def test_runtime_supervisor_lifecycle(tmp_path: Path) -> None:
    config = _make_config(tmp_path)
    service, _state, _ = _make_service(config)

    # 1. Normal execution
    executed = False

    async def normal_task() -> None:
        nonlocal executed
        executed = True

    await service.supervise("normal", normal_task)
    assert executed is True

    # 2. Fatal / Retry exhaustion error handling
    async def failing_task() -> None:
        raise RuntimeError("simulated fatal error")

    with pytest.raises(RuntimeError):
        await service.supervise("failing", failing_task)

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
    assert not mock_stream.send_message.called

    service.cleanup()


@pytest.mark.asyncio
async def test_runtime_unsupported_mcu_request(tmp_path: Path) -> None:
    config = _make_config(tmp_path)
    service, _state, mock_serial = _make_service(config)

    res = await service._unsupported_mcu_request(1, None, "unsupported_test")
    assert res is True
    assert mock_serial.acknowledge.called

    # When serial is None
    service.serial = None
    mock_serial.reset_mock()
    res = await service._unsupported_mcu_request(1, None, "unsupported_test")
    assert res is False

    service.cleanup()


@pytest.mark.asyncio
async def test_runtime_on_mcu_analog_read_resp(tmp_path: Path) -> None:
    config = _make_config(tmp_path)
    service, state, _ = _make_service(config)

    state.pending_analog_reads.append(PendingPinRequest(pin=0, reply_context=None))
    resp = pb.AnalogReadResponse(value=512)
    await service._on_mcu_analog_read_resp(1, resp)

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
    await service._on_mcu_process_kill(1, kill_req)

    assert 1 not in state.running_processes

    service.cleanup()


@pytest.mark.asyncio
async def test_runtime_handle_mcu_status_payloads(tmp_path: Path, mocker: MockerFixture) -> None:
    config = _make_config(tmp_path)
    service, state, _ = _make_service(config)

    mock_enqueue = mocker.patch.object(service, "enqueue_cloud", new_callable=AsyncMock)
    # 1. ProtobufMessage payload
    msg = pb.GenericResponse(message="test_msg")
    await service._handle_mcu_status(Status.ERROR, 1, msg)
    assert mock_enqueue.call_count == 1

    # 2. Raw bytes payload with valid Protobuf
    b_msg = pb.GenericResponse(message="bytes_msg").SerializeToString()
    await service._handle_mcu_status(Status.TIMEOUT, 2, b_msg)
    assert mock_enqueue.call_count == 2

    # 3. Corrupted raw bytes
    await service._handle_mcu_status(Status.MALFORMED, 3, b"\xff\xff\xff")
    assert mock_enqueue.call_count == 3

    service.cleanup()


@pytest.mark.asyncio
async def test_runtime_enqueue_cloud_drop(tmp_path: Path) -> None:
    config = _make_config(tmp_path)
    service, state, _ = _make_service(config)

    state.cloud_queue_limit = 1
    # Fill queue
    await service.enqueue_cloud(pb.CloudQueuedPublish(topic_name="test1", payload=b"1"))
    # Trigger drop
    await service.enqueue_cloud(pb.CloudQueuedPublish(topic_name="test2", payload=b"2"))

    assert state.cloud_dropped_messages > 0

    service.cleanup()


@pytest.mark.asyncio
async def test_runtime_console_queues_distribution(tmp_path: Path) -> None:
    config = _make_config(tmp_path)
    service, state, _ = _make_service(config)

    q1: asyncio.Queue[pb.CloudQueuedPublish] = asyncio.Queue()
    service.console_queues.append(q1)

    console_msg = pb.ConsoleWrite(data=b"console_output")
    await service._on_mcu_console_write(1, console_msg)

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
    handshake._parse_capabilities(cap_proto)
    assert isinstance(state.mcu_capabilities, pb.Capabilities) and state.mcu_capabilities.watchdog is True

    fut: asyncio.Future[Any] = asyncio.get_running_loop().create_future()
    handshake._capabilities_future = fut
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
    valid_tag = handshake.calculate_handshake_tag(config.serial_shared_secret, b"expected_nonce")
    state.link_expected_tag = valid_tag
    good_sync = pb.LinkSync(nonce=b"expected_nonce", tag=valid_tag)
    await handshake.handle_link_sync_resp(1, good_sync)
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

    metrics_snap = state.build_metrics_snapshot()
    msg = _build_metrics_message(state, metrics_snap, expiry_seconds=30.0)

    assert msg.topic_name == f"{config.topic_prefix}/metrics"
    assert len(msg.payload) > 0
    assert msg.user_property.get("device_id") == state.device_id

    state.cleanup()


@pytest.mark.asyncio
async def test_emit_bridge_snapshot_flavors(tmp_path: Path) -> None:
    config = _make_config(tmp_path)
    state = create_runtime_state(config)

    mock_enqueue = AsyncMock()

    # 1. Summary
    await _emit_bridge_snapshot(state, mock_enqueue, flavor="summary")
    assert mock_enqueue.called

    # 2. Handshake
    mock_enqueue.reset_mock()
    await _emit_bridge_snapshot(state, mock_enqueue, flavor="handshake")
    assert mock_enqueue.called

    # 3. Invalid flavor (noop)
    mock_enqueue.reset_mock()
    await _emit_bridge_snapshot(state, mock_enqueue, flavor="unknown")
    assert not mock_enqueue.called

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
        publish_bridge_snapshots(state, failing_enqueue, summary_interval=0.01, handshake_interval=0.01, min_interval=0.01)
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
    await service._on_mcu_console_write(1, msg)
    assert len(service.console_queues) == 0

    # 2. Digital Read Response
    state.pending_digital_reads.append(PendingPinRequest(pin=13, reply_context=None))
    resp = pb.DigitalReadResponse(value=1)
    await service._on_mcu_digital_read_resp(2, resp)
    assert len(state.pending_digital_reads) == 0

    # 3. Analog Read Response
    state.pending_analog_reads.append(PendingPinRequest(pin=0, reply_context=None))
    ana_resp = pb.AnalogReadResponse(value=512)
    await service._on_mcu_analog_read_resp(3, ana_resp)
    assert len(state.pending_analog_reads) == 0

    # 4. XON / XOFF flow control
    await service._handle_mcu_xoff(4, b"")
    assert state.mcu_is_paused
    assert not state.serial_tx_allowed.is_set()

    await service._handle_mcu_xon(5, b"")
    assert not state.mcu_is_paused
    assert state.serial_tx_allowed.is_set()

    # 5. Datastore put / get
    await service._on_mcu_datastore_put(6, pb.DatastorePut(key="k1", value=b"v1"))
    await service._on_mcu_datastore_get(7, pb.DatastoreGet(key="k1"))
    assert mock_serial.send.called

    service.cleanup()


@pytest.mark.asyncio
async def test_service_publish_cloud_message_flavors(tmp_path: Path) -> None:
    config = _make_config(tmp_path)
    service, _state, _ = _make_service(config)

    mock_stream = AsyncMock()
    service._cloud_stream = mock_stream

    # 1. Direct publish
    msg = pb.CloudQueuedPublish(topic_name="test/topic", payload=b"payload")
    res = await service._publish_cloud_message(msg)
    assert res is True
    assert mock_stream.send_message.called

    # 2. Stream None returns False
    service._cloud_stream = None
    res_none = await service._publish_cloud_message(msg)
    assert res_none is False

    # 3. Stream raises exception returns False
    service._cloud_stream = mock_stream
    mock_stream.send_message.side_effect = OSError("send failed")
    res_err = await service._publish_cloud_message(msg)
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
    await service._handle_mailbox(route_w, inbound_w)
    assert mock_serial.send.called
    assert len(state.mailbox_queue) == 1

    # 2. Mailbox Read (Empty)
    route_r = TopicRoute(
        raw="test/br/mailbox/read", prefix=config.topic_prefix, topic=Topic.MAILBOX, segments=("read",)
    )
    inbound_r = pb.CloudQueuedPublish(topic_name="test/br/mailbox/read", payload=b"")
    await service._handle_mailbox(route_r, inbound_r)

    # 3. Mailbox Read (Non-empty)
    await state.mailbox_incoming_queue.append(b"incoming_data")
    await service._handle_mailbox(route_r, inbound_r)

    service.cleanup()


@pytest.mark.asyncio
async def test_runtime_mcu_file_read_and_timeouts(tmp_path: Path) -> None:
    config = _make_config(tmp_path)
    service, state, mock_serial = _make_service(config)

    inbound = pb.CloudQueuedPublish(topic_name="test/br/file/read", payload=b"mcu:test.txt")

    # 1. Send failure
    mock_serial.send_raw.return_value = False
    await service._handle_file_mcu_read(inbound, "mcu:test.txt")
    assert mock_serial.send_raw.called

    # 2. Timeout waiting for response
    mock_serial.send_raw.return_value = True
    state.serial_response_timeout_ms = 10
    await service._handle_file_mcu_read(inbound, "mcu:test.txt")
    assert service._pending_mcu_read is None

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
    mock_run = mocker.patch.object(service, "_run_process", new_callable=AsyncMock, return_value=123)
    await service._handle_shell(route_run, inbound_run)
    assert mock_run.called

    # 2. Shell run async with error
    mock_err_run = mocker.patch.object(service, "_run_process", side_effect=OSError("spawn error"))
    await service._handle_shell(route_run, inbound_run)
    assert mock_err_run.called

    # 3. Shell poll
    mock_proc = MagicMock()
    ctx = ProcessContext(handle=mock_proc)
    state.running_processes[123] = ctx

    route_poll = TopicRoute(
        raw="test/br/shell/poll/123", prefix=config.topic_prefix, topic=Topic.SHELL, segments=("poll", "123")
    )
    inbound_poll = pb.CloudQueuedPublish(topic_name="test/br/shell/poll/123", payload=b"")
    mock_poll = mocker.patch.object(service, "_poll_process", new_callable=AsyncMock)
    mock_poll.return_value = pb.ProcessPollResponse(status=Status.OK.value, exit_code=0, finished=True)
    await service._handle_shell(route_poll, inbound_poll)
    assert mock_poll.called

    # 4. Shell kill
    route_kill = TopicRoute(
        raw="test/br/shell/kill/123", prefix=config.topic_prefix, topic=Topic.SHELL, segments=("kill", "123")
    )
    inbound_kill = pb.CloudQueuedPublish(topic_name="test/br/shell/kill/123", payload=b"")
    mock_term = mocker.patch.object(service, "_terminate_process", new_callable=AsyncMock, return_value=0)
    await service._handle_shell(route_kill, inbound_kill)
    assert mock_term.called

    service.cleanup()


@pytest.mark.asyncio
async def test_runtime_console_flush_and_queues(tmp_path: Path) -> None:
    config = _make_config(tmp_path)
    service, state, mock_serial = _make_service(config)

    state.connection_fsm.synchronize()

    # 1. Flush console queue
    state.console_to_mcu_queue.append(b"console_chunk")
    await service._flush_console_queue()
    assert mock_serial.send.called

    # 2. Flush console queue when serial send fails
    mock_serial.send.return_value = False
    state.console_to_mcu_queue.append(b"fail_chunk")
    await service._flush_console_queue()
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

    mock_spool = AsyncMock(spec=LmdbDeque)
    mock_spool.length = AsyncMock(side_effect=[2, 1, 0, 0, 0, 0])
    mock_spool.peek = AsyncMock(side_effect=[b"\xff\xffinvalid_protobuf", b""])
    mock_spool.popleft = AsyncMock()

    service._cloud_spool = mock_spool
    service._cloud_stream = AsyncMock()

    await service._flush_cloud_spool_locked()
    assert state.cloud_spool_corrupt_dropped > 0

    service.cleanup()
