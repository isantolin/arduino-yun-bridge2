# pyright: reportPrivateUsage=false
"""Phase 3 Comprehensive SIL-2 Coverage Hardening Test Suite.

Targets 95%+ line and branch coverage across mcubridge runtime, handshake, metrics,
transport, daemon, and gateway services without compromising SIL-2 test integrity.
"""

from __future__ import annotations

import asyncio
import tempfile
from pathlib import Path
from typing import Any, cast
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from grpclib.server import Stream

import mcubridge.protocol.mcubridge_pb2 as pb
from mcubridge.config.settings import RuntimeConfig
from mcubridge.metrics import (
    PrometheusExporter,
    RuntimeStateCollector,
    _build_metrics_message,
    _emit_bridge_snapshot,
    publish_bridge_snapshots,
    publish_metrics,
)
from mcubridge.protocol.protocol import (
    Command,
    Status,
    Topic,
)
from mcubridge.protocol.structures import PendingPinRequest, TopicRoute
from mcubridge.services.runtime import BridgeService, LocalBridgeService
from mcubridge.state.context import ProcessContext, RuntimeState, create_runtime_state
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


def _make_service(config: RuntimeConfig) -> tuple[BridgeService, RuntimeState, AsyncMock]:
    state = create_runtime_state(config)
    mock_serial = AsyncMock(spec=SerialTransport)
    mock_serial.send = AsyncMock(return_value=True)
    mock_serial.send_raw = AsyncMock(return_value=True)
    mock_serial.acknowledge = AsyncMock()
    mock_serial.is_open = True
    service = BridgeService(config=config, state=state, serial=mock_serial)
    return service, state, mock_serial


# ══════════════════════════════════════════════════════════════════════════════
# 1. Runtime Service: File, Shell, Pin, SPI, System & IPC
# ══════════════════════════════════════════════════════════════════════════════


@pytest.mark.asyncio
async def test_runtime_file_dispatch_handlers(tmp_path: Path) -> None:
    config = _make_config(tmp_path)
    service, state, mock_serial = _make_service(config)

    test_file = tmp_path / "hello.txt"
    test_file.write_text("hello world")

    inbound = pb.CloudQueuedPublish(
        topic_name=f"{config.topic_prefix}/file/read",
        payload=b"test data",
        correlation_data=b"cor123",
    )

    await service._file_dispatch_local_read("hello.txt", inbound, test_file)
    assert state.cloud_publish_queue.qsize() == 0

    await service._file_dispatch_local_write("hello2.txt", inbound, tmp_path / "hello2.txt")
    assert (tmp_path / "hello2.txt").exists()

    await service._file_dispatch_local_remove("hello2.txt", inbound, tmp_path / "hello2.txt")
    assert not (tmp_path / "hello2.txt").exists()

    mock_serial.send.reset_mock()
    await service._file_dispatch_mcu_write("mcu:test.txt", inbound, None)
    assert mock_serial.send.called

    mock_serial.send.reset_mock()
    await service._file_dispatch_mcu_remove("mcu:test.txt", inbound, None)
    assert mock_serial.send.called

    state.cleanup()


@pytest.mark.asyncio
async def test_runtime_pin_handlers(tmp_path: Path) -> None:
    config = _make_config(tmp_path)
    service, state, mock_serial = _make_service(config)

    # 1. Digital Pin Mode
    route_mode = TopicRoute(
        raw="test/br/d/13/mode", prefix=config.topic_prefix, topic=Topic.DIGITAL, segments=("13", "mode")
    )
    inbound_mode = pb.CloudQueuedPublish(topic_name="test/br/d/13/mode", payload=b"1")
    await service._handle_pin(route_mode, inbound_mode)
    mock_serial.send.assert_called_with(Command.CMD_SET_PIN_MODE.value, pb.PinMode(pin=13, mode=cast(Any, 1)))

    # 2. Digital Pin Read
    route_read = TopicRoute(
        raw="test/br/d/13/read", prefix=config.topic_prefix, topic=Topic.DIGITAL, segments=("13", "read")
    )
    inbound_read = pb.CloudQueuedPublish(topic_name="test/br/d/13/read", payload=b"")
    await service._handle_pin(route_read, inbound_read)
    mock_serial.send.assert_called_with(Command.CMD_DIGITAL_READ.value, pb.PinRead(pin=13))

    # 3. Digital Pin Write
    route_write = TopicRoute(raw="test/br/d/13", prefix=config.topic_prefix, topic=Topic.DIGITAL, segments=("13",))
    inbound_write = pb.CloudQueuedPublish(topic_name="test/br/d/13", payload=b"1")
    await service._handle_pin(route_write, inbound_write)
    mock_serial.send.assert_called_with(Command.CMD_DIGITAL_WRITE.value, pb.DigitalWrite(pin=13, value=1))

    # 4. Analog Pin Read Overflow
    state.pending_pin_request_limit = 1
    state.pending_analog_reads.append(PendingPinRequest(pin=1, reply_context=None))
    route_ana_read = TopicRoute(
        raw="test/br/a/1/read", prefix=config.topic_prefix, topic=Topic.ANALOG, segments=("1", "read")
    )
    inbound_ana_read = pb.CloudQueuedPublish(topic_name="test/br/a/1/read", payload=b"")
    await service._handle_pin(route_ana_read, inbound_ana_read)

    state.cleanup()


@pytest.mark.asyncio
async def test_runtime_spi_handlers(tmp_path: Path) -> None:
    config = _make_config(tmp_path)
    service, state, mock_serial = _make_service(config)

    # 1. SPI Begin
    route_begin = TopicRoute(raw="test/br/spi/begin", prefix=config.topic_prefix, topic=Topic.SPI, segments=("begin",))
    inbound = pb.CloudQueuedPublish(topic_name="test/br/spi/begin", payload=b"")
    await service._handle_spi(route_begin, inbound)
    mock_serial.send.assert_called_with(Command.CMD_SPI_BEGIN.value, b"")

    # 2. SPI End
    route_end = TopicRoute(raw="test/br/spi/end", prefix=config.topic_prefix, topic=Topic.SPI, segments=("end",))
    await service._handle_spi(route_end, inbound)
    mock_serial.send.assert_called_with(Command.CMD_SPI_END.value, b"")

    # 3. SPI Config
    route_cfg = TopicRoute(raw="test/br/spi/config", prefix=config.topic_prefix, topic=Topic.SPI, segments=("config",))
    cfg_proto = pb.SpiConfig(bit_order=1, data_mode=2, frequency=1000000)
    inbound_cfg = pb.CloudQueuedPublish(topic_name="test/br/spi/config", payload=cfg_proto.SerializeToString())
    await service._handle_spi(route_cfg, inbound_cfg)
    mock_serial.send.assert_called_with(Command.CMD_SPI_SET_CONFIG.value, cfg_proto)

    # 4. SPI Transfer
    mock_serial.send.return_value = pb.SpiTransferResponse(data=b"pong").SerializeToString()
    route_xfer = TopicRoute(
        raw="test/br/spi/transfer", prefix=config.topic_prefix, topic=Topic.SPI, segments=("transfer",)
    )
    inbound_xfer = pb.CloudQueuedPublish(topic_name="test/br/spi/transfer", payload=b"ping")
    await service._handle_spi(route_xfer, inbound_xfer)

    state.cleanup()


@pytest.mark.asyncio
async def test_runtime_system_handlers(tmp_path: Path) -> None:
    config = _make_config(tmp_path)
    service, state, mock_serial = _make_service(config)

    # 1. System Bootloader
    route_boot = TopicRoute(
        raw="test/br/system/bootloader", prefix=config.topic_prefix, topic=Topic.SYSTEM, segments=("bootloader",)
    )
    inbound = pb.CloudQueuedPublish(topic_name="test/br/system/bootloader", payload=b"")
    await service._handle_system(route_boot, inbound)
    assert mock_serial.send.called

    # 2. System Free Memory
    mock_serial.send.return_value = pb.FreeMemoryResponse(value=1024).SerializeToString()
    route_mem = TopicRoute(
        raw="test/br/system/free_memory/get",
        prefix=config.topic_prefix,
        topic=Topic.SYSTEM,
        segments=("free_memory", "get"),
    )
    await service._handle_system(route_mem, inbound)

    # 3. System Bridge Summary
    route_sum = TopicRoute(
        raw="test/br/system/bridge/summary",
        prefix=config.topic_prefix,
        topic=Topic.SYSTEM,
        segments=("bridge", "summary"),
    )
    await service._handle_system(route_sum, inbound)

    # 4. System Bridge Handshake
    route_hs = TopicRoute(
        raw="test/br/system/bridge/handshake",
        prefix=config.topic_prefix,
        topic=Topic.SYSTEM,
        segments=("bridge", "handshake"),
    )
    await service._handle_system(route_hs, inbound)

    state.cleanup()


@pytest.mark.asyncio
async def test_runtime_cloud_spool_operations(tmp_path: Path) -> None:
    config = _make_config(tmp_path)
    config.cloud_queue_limit = 2
    service, state, _ = _make_service(config)

    # Initialize spool
    service._cloud_spool = LmdbDeque(path=str(tmp_path / "spool_test"), maxlen=2)

    # 1. Spool message
    msg1 = pb.CloudQueuedPublish(topic_name="test/topic/1", payload=b"p1")
    res1 = await service._spool_cloud_message_locked(msg1)
    assert res1 is True
    assert state.cloud_spool_pending_messages == 1

    msg2 = pb.CloudQueuedPublish(topic_name="test/topic/2", payload=b"p2")
    await service._spool_cloud_message_locked(msg2)

    # Overwrite beyond limit
    msg3 = pb.CloudQueuedPublish(topic_name="test/topic/3", payload=b"p3")
    await service._spool_cloud_message_locked(msg3)
    assert state.cloud_spool_dropped_limit > 0

    # 2. Flush spool with mock stream
    mock_stream = AsyncMock()
    mock_stream.send_message = AsyncMock(return_value=True)
    service._cloud_stream = mock_stream
    service._publish_cloud_message = AsyncMock(return_value=True)

    await service._flush_cloud_spool_locked()
    assert state.cloud_spool_pending_messages == 0

    if service._cloud_spool:
        await service._cloud_spool.close()
    state.cleanup()


@pytest.mark.asyncio
async def test_runtime_supervisor_lifecycle(tmp_path: Path) -> None:
    config = _make_config(tmp_path)
    service, state, _ = _make_service(config)

    # 1. Normal execution
    executed = False

    async def sample_task() -> None:
        nonlocal executed
        executed = True

    await service.supervise("sample_task", sample_task, max_restarts=1)
    assert executed

    # 2. Task with handled exception and retry
    runs = 0

    async def failing_task() -> None:
        nonlocal runs
        runs += 1
        if runs < 2:
            raise OSError("temporary io failure")

    await service.supervise("failing_task", failing_task, max_restarts=3, min_backoff=0.001, max_backoff=0.01)
    assert runs == 2

    # 3. Fatal exception handling
    async def fatal_task() -> None:
        raise ValueError("unrecoverable configuration")

    with pytest.raises(ValueError):
        await service.supervise("fatal_task", fatal_task, fatal_exceptions=(ValueError,))

    # 4. Cancellation handling
    async def cancelled_task() -> None:
        await asyncio.sleep(10.0)

    task_coro = asyncio.create_task(service.supervise("cancelled_task", cancelled_task))
    await asyncio.sleep(0.01)
    task_coro.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task_coro

    state.cleanup()


@pytest.mark.asyncio
async def test_local_bridge_grpc_service(tmp_path: Path) -> None:
    config = _make_config(tmp_path)
    service, state, _ = _make_service(config)
    local_service = LocalBridgeService(service)

    # 1. Publish with response
    mock_stream = AsyncMock(spec=Stream)
    request = pb.CloudQueuedPublish(
        topic_name=f"{config.topic_prefix}/system/version/get",
        payload=b"",
        correlation_data=b"correlate_123",
    )
    mock_stream.recv_message.return_value = request

    # Simulate background reply to correlation
    async def reply_correlate() -> None:
        while b"correlate_123" not in service.ipc_requests:
            await asyncio.sleep(0.001)
        resp = pb.CloudQueuedPublish(topic_name="reply/test", payload=b"reply_payload")
        service.ipc_requests[b"correlate_123"].put_nowait(resp)

    t = asyncio.create_task(reply_correlate())
    await local_service.Publish(mock_stream)
    await t
    assert mock_stream.send_message.called

    # 2. Publish without correlation
    mock_stream.reset_mock()
    request_no_cor = pb.CloudQueuedPublish(
        topic_name=f"{config.topic_prefix}/system/version/get",
        payload=b"",
    )
    mock_stream.recv_message.return_value = request_no_cor
    await local_service.Publish(mock_stream)
    assert mock_stream.send_message.called

    # 3. Publish when recv_message returns None
    mock_stream.reset_mock()
    mock_stream.recv_message.return_value = None
    await local_service.Publish(mock_stream)
    assert not mock_stream.send_message.called

    state.cleanup()


@pytest.mark.asyncio
async def test_runtime_unsupported_mcu_request(tmp_path: Path) -> None:
    config = _make_config(tmp_path)
    service, state, mock_serial = _make_service(config)

    res = await service._unsupported_mcu_request(1, None, "unsupported_test")
    assert res is True
    mock_serial.send.assert_called_with(
        Status.NOT_IMPLEMENTED.value,
        pb.GenericResponse(message="unsupported_test"),
    )

    # When serial is None
    service.serial = None
    res = await service._unsupported_mcu_request(1, None, "unsupported_test")
    assert res is False

    state.cleanup()


@pytest.mark.asyncio
async def test_runtime_on_mcu_analog_read_resp(tmp_path: Path) -> None:
    config = _make_config(tmp_path)
    service, state, _ = _make_service(config)

    # Register a pending analog read waiter
    state.pending_analog_reads.append(PendingPinRequest(pin=1, reply_context=None))

    resp = pb.AnalogReadResponse(value=512)
    await service._on_mcu_analog_read_resp(1, resp)

    assert len(state.pending_analog_reads) == 0

    state.cleanup()


@pytest.mark.asyncio
async def test_runtime_on_mcu_process_kill(tmp_path: Path) -> None:
    config = _make_config(tmp_path)
    service, state, _ = _make_service(config)

    mock_proc = MagicMock()
    mock_proc.pid = 9999
    mock_proc.returncode = None
    mock_proc.terminate = MagicMock()
    mock_proc.kill = MagicMock()
    mock_proc.wait = AsyncMock(return_value=0)

    ctx = ProcessContext(handle=mock_proc)
    state.running_processes[1] = ctx

    kill_cmd = pb.ProcessKill(pid=1)
    await service._on_mcu_process_kill(1, kill_cmd)

    assert 1 not in state.running_processes

    state.cleanup()


@pytest.mark.asyncio
async def test_runtime_handle_mcu_status_payloads(tmp_path: Path) -> None:
    config = _make_config(tmp_path)
    service, state, _ = _make_service(config)

    # 1. ProtobufMessage payload
    msg = pb.GenericResponse(message="test_msg")
    await service._handle_mcu_status(Status.ERROR, 1, msg)

    # 2. Raw bytes payload with valid Protobuf
    b_msg = pb.GenericResponse(message="bytes_msg").SerializeToString()
    await service._handle_mcu_status(Status.TIMEOUT, 2, b_msg)

    # 3. Corrupted raw bytes
    await service._handle_mcu_status(Status.MALFORMED, 3, b"\xff\xff\xff")

    state.cleanup()


@pytest.mark.asyncio
async def test_runtime_enqueue_cloud_drop(tmp_path: Path) -> None:
    config = _make_config(tmp_path)
    service, state, _ = _make_service(config)

    # Calling enqueue_cloud when cloud is offline and spool is not initialized/full
    service._cloud_spool = None
    msg = pb.CloudQueuedPublish(topic_name=f"{config.topic_prefix}/test", payload=b"payload")
    await service.enqueue_cloud(msg)

    assert state.cloud_dropped_messages > 0

    state.cleanup()


@pytest.mark.asyncio
async def test_runtime_console_queues_distribution(tmp_path: Path) -> None:
    config = _make_config(tmp_path)
    service, state, _ = _make_service(config)

    q1: asyncio.Queue[pb.CloudQueuedPublish] = asyncio.Queue()
    service.console_queues.append(q1)

    msg = pb.CloudQueuedPublish(topic_name=f"{config.topic_prefix}/console/tx", payload=b"console_output")
    await service.enqueue_cloud(msg)

    assert q1.qsize() == 1
    received = await q1.get()
    assert received.payload == b"console_output"

    service.console_queues.remove(q1)
    state.cleanup()


# ══════════════════════════════════════════════════════════════════════════════
# 2. Handshake Service: Edge Cases & Error Branches
# ══════════════════════════════════════════════════════════════════════════════


@pytest.mark.asyncio
async def test_handshake_handle_capabilities_resp(tmp_path: Path) -> None:
    config = _make_config(tmp_path)
    service, state, _ = _make_service(config)
    handshake = service.handshake

    cap_proto = pb.Capabilities(
        ver=2,
        arch=1,
        dig=20,
        ana=6,
        watchdog=True,
    )
    handshake._parse_capabilities(cap_proto)
    assert isinstance(state.mcu_capabilities, pb.Capabilities) and state.mcu_capabilities.ver == 2

    fut: asyncio.Future[Any] = asyncio.get_running_loop().create_future()
    handshake._capabilities_future = fut
    await handshake.handle_capabilities_resp(1, cap_proto)
    assert fut.done()
    assert fut.result() == cap_proto

    state.cleanup()


@pytest.mark.asyncio
async def test_handshake_handle_link_sync_resp(tmp_path: Path) -> None:
    config = _make_config(tmp_path)
    service, state, _ = _make_service(config)
    handshake = service.handshake

    nonce = b"1234567812345678"
    state.link_handshake_nonce = nonce

    # 1. Mismatched nonce
    bad_sync = pb.LinkSync(nonce=b"8765432187654321", tag=b"8765432187654321")
    await handshake.handle_link_sync_resp(1, bad_sync)
    assert not state.is_synchronized

    # 2. Matching nonce and valid recalculated tag
    state.link_handshake_nonce = nonce
    expected_tag = handshake.calculate_handshake_tag(config.serial_shared_secret, nonce)
    state.link_expected_tag = expected_tag
    good_sync = pb.LinkSync(nonce=nonce, tag=expected_tag)
    await handshake.handle_link_sync_resp(1, good_sync)
    assert state.is_synchronized

    state.cleanup()


@pytest.mark.asyncio
async def test_handshake_handle_link_reset_resp(tmp_path: Path) -> None:
    config = _make_config(tmp_path)
    service, state, _ = _make_service(config)
    handshake = service.handshake

    res = await handshake.handle_link_reset_resp(1, pb.GenericResponse(message="reset_ok"))
    assert res is True

    state.cleanup()


# ══════════════════════════════════════════════════════════════════════════════
# 3. Metrics Collector & Prometheus Exporter
# ══════════════════════════════════════════════════════════════════════════════


def test_runtime_state_collector(tmp_path: Path) -> None:
    config = _make_config(tmp_path)
    state = create_runtime_state(config)
    collector = RuntimeStateCollector(state)

    state.mark_transport_connected()
    state.mark_synchronized()
    state.file_storage_bytes_used = 1024

    metrics = list(collector.collect())
    assert len(metrics) > 0

    metric_names: list[str] = [str(getattr(m, "name", getattr(m, "_name", ""))) for m in metrics]
    assert "mcubridge_queue_depth" in metric_names
    assert "mcubridge_file_storage_bytes_used" in metric_names
    assert "mcubridge_link_synchronized" in metric_names

    state.cleanup()


def test_build_metrics_message_with_extra_props(tmp_path: Path) -> None:
    config = _make_config(tmp_path)
    state = create_runtime_state(config)

    # 1. Standard metrics message
    snapshot = state.build_metrics_snapshot()
    msg = _build_metrics_message(state, snapshot, expiry_seconds=30.0)
    assert msg.topic_name.endswith("/system/metrics")

    # 2. Degraded spool and quota limit properties
    snapshot.cloud_spool_degraded = True
    snapshot.cloud_spool_failure_reason = "disk_full"
    state.file_storage_limit_rejections = 5

    msg_extra = _build_metrics_message(state, snapshot, expiry_seconds=30.0)
    keys = [p.key for p in msg_extra.user_properties]
    assert "bridge-spool" in keys
    assert "bridge-files" in keys

    # 3. Write limit rejection
    state.file_storage_limit_rejections = 0
    state.file_write_limit_rejections = 3
    msg_write_limit = _build_metrics_message(state, snapshot, expiry_seconds=30.0)
    keys2 = [p.key for p in msg_write_limit.user_properties]
    assert "bridge-files" in keys2

    state.cleanup()


@pytest.mark.asyncio
async def test_emit_bridge_snapshot_flavors(tmp_path: Path) -> None:
    config = _make_config(tmp_path)
    state = create_runtime_state(config)

    enqueued: list[pb.CloudQueuedPublish] = []

    async def mock_enqueue(msg: pb.CloudQueuedPublish) -> None:
        enqueued.append(msg)

    # 1. Handshake flavor
    await _emit_bridge_snapshot(state, mock_enqueue, flavor="handshake")
    assert len(enqueued) == 1
    assert "handshake" in enqueued[0].topic_name

    # 2. Summary flavor
    await _emit_bridge_snapshot(state, mock_enqueue, flavor="summary")
    assert len(enqueued) == 2
    assert "summary" in enqueued[1].topic_name

    state.cleanup()


@pytest.mark.asyncio
async def test_publish_metrics_error_handling(tmp_path: Path) -> None:
    config = _make_config(tmp_path)
    state = create_runtime_state(config)

    call_count = 0

    async def failing_enqueue(msg: pb.CloudQueuedPublish) -> None:
        nonlocal call_count
        call_count += 1
        if call_count == 1:
            raise OSError("network io error")
        await asyncio.sleep(0.01)

    task = asyncio.create_task(publish_metrics(state, failing_enqueue, interval=0.01, min_interval=0.01))
    await asyncio.sleep(0.05)
    task.cancel()
    try:
        await task
    except asyncio.CancelledError:
        pass

    assert call_count > 0
    state.cleanup()


@pytest.mark.asyncio
async def test_publish_bridge_snapshots_error_handling(tmp_path: Path) -> None:
    config = _make_config(tmp_path)
    state = create_runtime_state(config)

    call_count = 0

    async def failing_enqueue(msg: pb.CloudQueuedPublish) -> None:
        nonlocal call_count
        call_count += 1
        if call_count == 1:
            raise RuntimeError("test error")
        await asyncio.sleep(0.01)

    task = asyncio.create_task(
        publish_bridge_snapshots(
            state,
            failing_enqueue,
            summary_interval=0.01,
            handshake_interval=0.01,
            min_interval=0.01,
        )
    )
    await asyncio.sleep(0.05)
    task.cancel()
    try:
        await task
    except asyncio.CancelledError:
        pass

    assert call_count > 0
    state.cleanup()


# ══════════════════════════════════════════════════════════════════════════════
# 4. Storage: LmdbDeque Error Paths & Capacity Limits
# ══════════════════════════════════════════════════════════════════════════════


@pytest.mark.asyncio
async def test_lmdb_deque_operations(tmp_path: Path) -> None:
    spool_path = str(tmp_path / "lmdb_test")
    deque = LmdbDeque(path=spool_path, maxlen=3)

    assert len(deque) == 0

    # Append items
    for i in range(3):
        await deque.append(f"payload_{i}".encode())

    assert len(deque) == 3

    # Append exceeding maxlen
    await deque.append(b"overflow")
    assert len(deque) == 3

    # Pop left
    item = await deque.popleft()
    assert item == b"payload_1"
    assert len(deque) == 2

    # Clear
    await deque.clear()
    assert len(deque) == 0

    await deque.close()


# ══════════════════════════════════════════════════════════════════════════════
# 5. Prometheus Exporter Lifecycle
# ══════════════════════════════════════════════════════════════════════════════


@pytest.mark.asyncio
async def test_prometheus_exporter_lifecycle(tmp_path: Path) -> None:
    config = _make_config(tmp_path)
    state = create_runtime_state(config)

    exporter = PrometheusExporter(state, host="127.0.0.1", port=0)
    assert exporter.port > 0

    task = asyncio.create_task(exporter.run())
    await asyncio.sleep(0.05)
    task.cancel()
    try:
        await task
    except asyncio.CancelledError:
        pass

    state.cleanup()


# ══════════════════════════════════════════════════════════════════════════════
# 6. Service Lifecycle, Telemetry Envelopes & MCU Frame Dispatch
# ══════════════════════════════════════════════════════════════════════════════


@pytest.mark.asyncio
async def test_service_serial_lifecycle(tmp_path: Path) -> None:
    config = _make_config(tmp_path)
    service, state, mock_serial = _make_service(config)

    # 1. On serial connected (synchronized)
    async def mock_sync_impl() -> bool:
        state.mark_synchronized()
        return True

    with patch.object(service.handshake, "synchronize", side_effect=mock_sync_impl) as mock_sync:
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
    with patch("pathlib.Path.unlink", side_effect=OSError("unlink error")):
        service.cleanup()

    state.cleanup()


@pytest.mark.asyncio
async def test_service_handle_mcu_frame_dispatch(tmp_path: Path) -> None:
    config = _make_config(tmp_path)
    service, state, mock_serial = _make_service(config)

    state.mark_synchronized()

    # 1. Registered MCU command
    msg = pb.ConsoleWrite(data=b"hello mcu")
    await service.handle_mcu_frame(Command.CMD_CONSOLE_WRITE.value, 1, msg.SerializeToString())
    assert mock_serial.acknowledge.called

    # 2. Status frame
    mock_serial.acknowledge.reset_mock()
    await service.handle_mcu_frame(Status.OK.value, 2, b"")
    assert not mock_serial.acknowledge.called

    # 3. Unknown command
    await service.handle_mcu_frame(0x999, 3, b"")
    assert cast(Any, state.metrics.unknown_command_count)._value.get() > 0

    # 4. XON / XOFF flow control
    await service._handle_mcu_xoff(4, b"")
    assert state.mcu_is_paused is True
    assert not state.serial_tx_allowed.is_set()

    await service._handle_mcu_xon(5, b"")
    assert state.mcu_is_paused is False
    assert state.serial_tx_allowed.is_set()

    # 5. Datastore put / get
    await service._on_mcu_datastore_put(6, pb.DatastorePut(key="k1", value=b"v1"))
    mock_serial.send.reset_mock()
    await service._on_mcu_datastore_get(7, pb.DatastoreGet(key="k1"))
    assert mock_serial.send.called

    state.cleanup()


@pytest.mark.asyncio
async def test_service_publish_cloud_message_flavors(tmp_path: Path) -> None:
    config = _make_config(tmp_path)
    service, state, _ = _make_service(config)

    mock_stream = AsyncMock()
    service._cloud_stream = mock_stream

    # 1. Metrics flavor
    m_msg = pb.CloudQueuedPublish(topic_name=f"{config.topic_prefix}/system/metrics", payload=b"metrics_data")
    res1 = await service._publish_cloud_message(m_msg)
    assert res1 is True

    # 2. Summary flavor
    s_msg = pb.CloudQueuedPublish(topic_name=f"{config.topic_prefix}/system/bridge/summary", payload=b"summary_data")
    res2 = await service._publish_cloud_message(s_msg)
    assert res2 is True

    # 3. Handshake flavor
    h_msg = pb.CloudQueuedPublish(topic_name=f"{config.topic_prefix}/system/bridge/handshake", payload=b"hs_data")
    res3 = await service._publish_cloud_message(h_msg)
    assert res3 is True

    # 4. Status flavor
    st_msg = pb.CloudQueuedPublish(topic_name=f"{config.topic_prefix}/system/status", payload=b"st_data")
    res4 = await service._publish_cloud_message(st_msg)
    assert res4 is True

    # 5. Stream error handling
    mock_stream.send_message.side_effect = OSError("network drop")
    res_err = await service._publish_cloud_message(m_msg)
    assert res_err is False

    state.cleanup()


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

    state.cleanup()


@pytest.mark.asyncio
async def test_runtime_mcu_file_read_and_timeouts(tmp_path: Path) -> None:
    config = _make_config(tmp_path)
    service, state, mock_serial = _make_service(config)

    inbound = pb.CloudQueuedPublish(topic_name="test/br/file/read", payload=b"mcu:test.txt")

    # 1. Send failure
    mock_serial.send_raw.return_value = False
    await service._file_dispatch_mcu_read("mcu:test.txt", inbound, None)

    # 2. Timeout waiting for response
    mock_serial.send_raw.return_value = True
    state.serial_response_timeout_ms = 10
    await service._file_dispatch_mcu_read("mcu:test.txt", inbound, None)

    state.cleanup()


@pytest.mark.asyncio
async def test_runtime_shell_dispatch_handlers(tmp_path: Path) -> None:
    config = _make_config(tmp_path)
    service, state, _ = _make_service(config)

    # 1. Shell run async
    route_run = TopicRoute(
        raw="test/br/shell/run_async", prefix=config.topic_prefix, topic=Topic.SHELL, segments=("run_async",)
    )
    inbound_run = pb.CloudQueuedPublish(topic_name="test/br/shell/run_async", payload=b"echo hello")
    with patch.object(service, "_run_process", new_callable=AsyncMock, return_value=123):
        await service._handle_shell(route_run, inbound_run)

    # 2. Shell run async with error
    with patch.object(service, "_run_process", side_effect=OSError("spawn error")):
        await service._handle_shell(route_run, inbound_run)

    # 3. Shell poll
    mock_proc = MagicMock()
    ctx = ProcessContext(handle=mock_proc)
    state.running_processes[123] = ctx

    route_poll = TopicRoute(
        raw="test/br/shell/poll/123", prefix=config.topic_prefix, topic=Topic.SHELL, segments=("poll", "123")
    )
    inbound_poll = pb.CloudQueuedPublish(topic_name="test/br/shell/poll/123", payload=b"")
    with patch.object(service, "_poll_process", new_callable=AsyncMock) as mock_poll:
        mock_poll.return_value = pb.ProcessPollResponse(status=Status.OK.value, exit_code=0, finished=True)
        await service._handle_shell(route_poll, inbound_poll)

    # 4. Shell kill
    route_kill = TopicRoute(
        raw="test/br/shell/kill/123", prefix=config.topic_prefix, topic=Topic.SHELL, segments=("kill", "123")
    )
    inbound_kill = pb.CloudQueuedPublish(topic_name="test/br/shell/kill/123", payload=b"")
    with patch.object(service, "_terminate_process", new_callable=AsyncMock, return_value=0):
        await service._handle_shell(route_kill, inbound_kill)

    state.cleanup()


@pytest.mark.asyncio
async def test_runtime_console_flush_and_queues(tmp_path: Path) -> None:
    config = _make_config(tmp_path)
    service, state, mock_serial = _make_service(config)

    state.mark_synchronized()

    # 1. Flush console queue
    state.console_to_mcu_queue.append(b"console_chunk")
    await service._flush_console_queue()
    assert mock_serial.send.called

    # 2. Flush console queue when serial send fails
    mock_serial.send.return_value = False
    state.console_to_mcu_queue.append(b"fail_chunk")
    await service._flush_console_queue()
    assert len(state.console_to_mcu_queue) > 0

    state.cleanup()
