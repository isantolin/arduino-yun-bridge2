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

from hypothesis import given, settings, strategies as st
import pytest

from mcubridge.config.settings import RuntimeConfig
from mcubridge.metrics import (
    publish_bridge_snapshots,
    publish_metrics,
)
import mcubridge.protocol.mcubridge_pb2 as pb
from mcubridge.protocol.protocol import (
    PinAction,
    Status,
    Topic,
)
from mcubridge.protocol.structures import PendingPinRequest, TopicRoute
from mcubridge.services.handshake import SerialHandshakeManager
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
    mock_serial.is_open = True
    service = BridgeService(config, state, mock_serial)
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


# ══════════════════════════════════════════════════════════════════════════════
# 2. Pin, SPI & System Handlers
# ══════════════════════════════════════════════════════════════════════════════


@pytest.mark.asyncio
async def test_runtime_pin_handlers(tmp_path: Path) -> None:
    config = _make_config(tmp_path)
    service, state, _mock_serial = _make_service(config)
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


# ══════════════════════════════════════════════════════════════════════════════
# 3. Cloud Spooling & LMDB Persistence
# ══════════════════════════════════════════════════════════════════════════════


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


# ══════════════════════════════════════════════════════════════════════════════
# 4. Supervisor Task Lifecycle & IPC
# ══════════════════════════════════════════════════════════════════════════════


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


@settings(max_examples=30, derandomize=True, deadline=None)
@given(
    status=st.sampled_from(list(Status)),
    seq=st.integers(0, 65535),
    payload=st.one_of(
        st.binary(max_size=64),
        st.text(max_size=32).map(lambda s: pb.GenericResponse(message=s)),
    ),
)
def test_runtime_handle_mcu_status_payloads(
    tmp_path_factory: pytest.TempPathFactory,
    status: Status,
    seq: int,
    payload: bytes | pb.GenericResponse,
) -> None:
    async def _run() -> None:
        config = _make_config(Path(tmp_path_factory.mktemp("mcu_status")))
        service, _state, _ = _make_service(config)

        mock_enqueue = AsyncMock()
        service.enqueue_cloud = mock_enqueue
        handle_status_fn: Callable[..., Awaitable[None]] = getattr(service, "_handle_mcu_status")
        await handle_status_fn(status, seq, payload)
        assert mock_enqueue.await_count == 1
        call_args = mock_enqueue.await_args[0]
        queued_msg = call_args[0]
        assert isinstance(queued_msg, pb.CloudQueuedPublish)

        service.cleanup()

    asyncio.run(_run())


@pytest.mark.asyncio
async def test_runtime_enqueue_cloud_drop(tmp_path: Path) -> None:
    config = _make_config(tmp_path)
    config.cloud_enabled = True
    service, state, _ = _make_service(config)

    state.cloud_queue_limit = 1
    mock_spool_locked = AsyncMock(return_value=False)
    service._spool_cloud_message_locked = mock_spool_locked
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

    service.cleanup()


# ══════════════════════════════════════════════════════════════════════════════
# 5. Handshake Protocol & Link Synchronization
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


@settings(max_examples=25, derandomize=True, deadline=None)
@given(
    secret=st.binary(min_size=1, max_size=32),
    nonce=st.binary(min_size=8, max_size=16),
)
def test_handshake_calculate_tag_deterministic_property(secret: bytes, nonce: bytes) -> None:
    tag1 = SerialHandshakeManager.calculate_handshake_tag(secret, nonce)
    tag2 = SerialHandshakeManager.calculate_handshake_tag(secret, nonce)
    assert tag1 == tag2
    assert isinstance(tag1, bytes)
    assert len(tag1) > 0


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
    service, state, _ = _make_service(config)
    handshake = service.handshake

    fut: asyncio.Future[Any] = asyncio.get_running_loop().create_future()
    handshake._reset_future = fut

    await handshake.handle_link_reset_resp(1, None)
    assert fut.done()
    assert fut.result() is True

    service.cleanup()


# ══════════════════════════════════════════════════════════════════════════════
# 6. Metrics Construction & Snapping
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
    assert len(msg.user_properties) > 0

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
async def test_publish_bridge_snapshots_loop_error_recovery(tmp_path: Path) -> None:
    config = _make_config(tmp_path)
    state = create_runtime_state(config)

    async def failing_enqueue(msg: pb.CloudQueuedPublish) -> None:
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

    state.cleanup()


@settings(max_examples=25, derandomize=True, deadline=None)
@given(
    items=st.lists(st.binary(min_size=1, max_size=32), min_size=2, max_size=8),
)
def test_lmdb_deque_operations(tmp_path_factory: pytest.TempPathFactory, items: list[bytes]) -> None:
    async def _run() -> None:
        deque_path = str(tmp_path_factory.mktemp("deque_ops") / "test_deque")
        deque = LmdbDeque(path=deque_path, maxlen=len(items))

        for item in items:
            await deque.append(item)
        assert len(deque) == len(items)

        assert await deque.peek() == items[0]
        assert await deque.popleft() == items[0]
        assert len(deque) == len(items) - 1

        await deque.clear()
        assert len(deque) == 0

        with pytest.raises(IndexError):
            await deque.popleft()

        with pytest.raises(IndexError):
            await deque.peek()

        await deque.close()

    asyncio.run(_run())


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
    await service._handle_file_mcu_read(inbound, "mcu:test.txt")
    assert mock_serial.send_raw.called

    # 2. Timeout waiting for response
    mock_serial.send_raw.return_value = True
    state.serial_response_timeout_ms = 10
    await service._handle_file_mcu_read(inbound, "mcu:test.txt")
    assert service._pending_mcu_read is None

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
    mock_run = AsyncMock(return_value=123)
    service.run_process = mock_run
    handle_sh_fn: Callable[..., Awaitable[None]] = getattr(service, "_handle_shell")
    await handle_sh_fn(route_run, inbound_run)
    assert mock_run.called

    # 2. Shell run async with error
    mock_err_run = AsyncMock(side_effect=OSError("spawn error"))
    service.run_process = mock_err_run
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
    mock_poll = AsyncMock(return_value=pb.ProcessPollResponse(status=Status.OK.value, exit_code=0, finished=True))
    service.poll_process = mock_poll
    await handle_sh_fn(route_poll, inbound_poll)
    assert mock_poll.called

    # 4. Shell kill
    route_kill = TopicRoute(
        raw="test/br/shell/kill/123", prefix=config.topic_prefix, topic=Topic.SHELL, segments=("kill", "123")
    )
    inbound_kill = pb.CloudQueuedPublish(topic_name="test/br/shell/kill/123", payload=b"")
    mock_term = AsyncMock(return_value=0)
    service._terminate_process = mock_term
    await handle_sh_fn(route_kill, inbound_kill)
    assert mock_term.called

    state.cleanup()


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

    state.cleanup()


# ══════════════════════════════════════════════════════════════════════════════
# 8. Cloud Stream Session & Corrupt Spool Flush Hardening
# ══════════════════════════════════════════════════════════════════════════════


@pytest.mark.asyncio
async def test_runtime_flush_cloud_spool_corrupt_and_errors(tmp_path: Path) -> None:
    config = _make_config(tmp_path)
    service, state, _ = _make_service(config)

    mock_spool = MagicMock(spec=LmdbDeque)
    mock_spool.__len__.side_effect = [2, 1, 0, 0]
    mock_spool.peek = AsyncMock(side_effect=[b"\xff\xffinvalid_protobuf", b""])
    mock_spool.popleft = AsyncMock()

    service._cloud_spool = mock_spool
    service._cloud_stream = AsyncMock()

    await service._flush_cloud_spool_locked()
    assert state.cloud_spool_corrupt_dropped > 0

    state.cleanup()
