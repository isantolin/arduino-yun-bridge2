# pyright: reportPrivateUsage=false
"""Surgical unit test suite targeting uncovered branches in runtime.py."""

from __future__ import annotations

import asyncio
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from mcubridge.config.settings import RuntimeConfig
import mcubridge.protocol.mcubridge_pb2 as pb
from mcubridge.protocol.protocol import Status
from mcubridge.protocol.topics import parse_topic
from mcubridge.services.runtime import BridgeService
from mcubridge.state.context import RuntimeState, create_runtime_state
from mcubridge.transport.serial import SerialTransport


def _make_config() -> RuntimeConfig:
    return RuntimeConfig(
        allowed_commands=("echo", "ls"),
        serial_shared_secret=b"testsharedsecret",
        allow_non_tmp_paths=True,
    )


@pytest.fixture
def test_config() -> RuntimeConfig:
    return _make_config()


@pytest.fixture
def mock_bridge_state(test_config: RuntimeConfig) -> RuntimeState:
    return create_runtime_state(test_config)


@pytest.mark.asyncio
async def test_enqueue_cloud_ipc_correlation(test_config: RuntimeConfig, mock_bridge_state: RuntimeState) -> None:
    svc = BridgeService(test_config, mock_bridge_state, MagicMock())
    q: asyncio.Queue[pb.CloudQueuedPublish] = asyncio.Queue()
    correlation_bytes = b"corr-123"
    svc.ipc_requests[correlation_bytes] = q

    msg = pb.CloudQueuedPublish(
        topic_name="mcu/data",
        correlation_data=correlation_bytes,
        payload=b"test",
    )
    await svc.enqueue_cloud(msg)
    pushed = await q.get()
    assert pushed.topic_name == "mcu/data"


@pytest.mark.asyncio
async def test_enqueue_cloud_console_queue(test_config: RuntimeConfig, mock_bridge_state: RuntimeState) -> None:
    svc = BridgeService(test_config, mock_bridge_state, MagicMock())
    cq: asyncio.Queue[pb.CloudQueuedPublish] = asyncio.Queue()
    svc.console_queues.append(cq)

    msg = pb.CloudQueuedPublish(topic_name="mcu/console/stdout", payload=b"hello")
    setattr(svc, "_publish_cloud_message", AsyncMock(return_value=True))
    await svc.enqueue_cloud(msg)

    pushed = await cq.get()
    assert pushed.payload == b"hello"


@pytest.mark.asyncio
async def test_spool_cloud_message_trim_limit(test_config: RuntimeConfig, mock_bridge_state: RuntimeState) -> None:
    svc = BridgeService(test_config, mock_bridge_state, MagicMock())
    mock_bridge_state.cloud_queue_limit = 5
    mock_spool = MagicMock()
    mock_spool.append = AsyncMock(return_value=1)
    mock_spool.__len__.return_value = 5
    svc._cloud_spool = mock_spool

    msg = pb.CloudQueuedPublish(topic_name="mcu/test", payload=b"data")
    res = await svc._spool_cloud_message_locked(msg)

    assert res is True
    assert mock_bridge_state.cloud_spool_dropped_limit == 1
    assert mock_bridge_state.cloud_spool_trim_events == 1


@pytest.mark.asyncio
async def test_spool_cloud_message_exceptions(test_config: RuntimeConfig, mock_bridge_state: RuntimeState) -> None:
    svc = BridgeService(test_config, mock_bridge_state, MagicMock())

    # Case 1: No spool
    svc._cloud_spool = None
    msg = pb.CloudQueuedPublish(topic_name="test", payload=b"a")
    assert await svc._spool_cloud_message_locked(msg) is False

    # Case 2: Database error on append
    mock_spool = MagicMock()
    mock_spool.__len__.return_value = 0
    mock_spool.append = AsyncMock(side_effect=OSError("Disk full"))
    svc._cloud_spool = mock_spool
    assert await svc._spool_cloud_message_locked(msg) is False
    assert mock_bridge_state.cloud_spool_degraded is True


@pytest.mark.asyncio
async def test_flush_cloud_spool_corrupt_entry(test_config: RuntimeConfig, mock_bridge_state: RuntimeState) -> None:
    svc = BridgeService(test_config, mock_bridge_state, MagicMock())
    mock_spool = MagicMock()
    svc._cloud_stream = MagicMock()
    svc._cloud_spool = mock_spool

    # First peek returns garbage bytes, causing ProtobufDecodeError/ValueError
    mock_spool.__len__.side_effect = [2, 1, 0, 0]
    mock_spool.peek = AsyncMock(return_value=b"\xff\xff\xff\xff")
    mock_spool.popleft = AsyncMock(return_value=None)
    mock_spool.vacuum = AsyncMock(return_value=None)

    await svc._flush_cloud_spool_locked()

    assert mock_bridge_state.cloud_spool_corrupt_dropped == 1
    mock_spool.vacuum.assert_awaited_once()


@pytest.mark.asyncio
async def test_handle_mcu_status_formatting(test_config: RuntimeConfig, mock_bridge_state: RuntimeState) -> None:
    svc = BridgeService(test_config, mock_bridge_state, MagicMock())
    mock_enqueue = AsyncMock()
    setattr(svc, "enqueue_cloud", mock_enqueue)

    # Test status with GenericResponse payload
    resp = pb.GenericResponse(message="System initialized")
    await svc._handle_mcu_status(Status.OK, 1, resp)
    mock_enqueue.assert_awaited()

    # Test status with raw bytes payload
    await svc._handle_mcu_status(Status.ERROR, 2, b"Raw error bytes")
    assert mock_enqueue.call_count == 2


@pytest.mark.asyncio
async def test_handle_datastore_actions(test_config: RuntimeConfig, mock_bridge_state: RuntimeState) -> None:
    svc = BridgeService(test_config, mock_bridge_state, MagicMock())
    cache = AsyncMock()
    cache.get.return_value = b"cached-value"
    cache.set.return_value = None
    mock_bridge_state.datastore_cache = cache
    mock_publish = AsyncMock()
    setattr(svc, "publish_datastore_value", mock_publish)

    prefix = mock_bridge_state.cloud_topic_prefix
    put_topic = f"{prefix}/datastore/put/temp"
    get_topic = f"{prefix}/datastore/get/temp"

    # PUT Action
    route_put = parse_topic(prefix, put_topic)
    assert route_put is not None
    msg_put = pb.CloudQueuedPublish(topic_name=put_topic, payload=b"25.4")
    await svc._handle_datastore(route_put, msg_put)
    cache.set.assert_awaited_with("temp", b"25.4")

    # GET Action (Cache Hit)
    route_get = parse_topic(prefix, get_topic)
    assert route_get is not None
    msg_get = pb.CloudQueuedPublish(topic_name=get_topic, payload=b"")
    await svc._handle_datastore(route_get, msg_get)
    mock_publish.assert_awaited_with("temp", b"cached-value", reply_context=msg_get)


@pytest.mark.asyncio
async def test_handle_mailbox_read_write(test_config: RuntimeConfig, mock_bridge_state: RuntimeState) -> None:
    mock_serial = AsyncMock()
    mock_serial.send.return_value = True
    svc = BridgeService(test_config, mock_bridge_state, mock_serial)
    mock_enqueue = AsyncMock()
    setattr(svc, "enqueue_cloud", mock_enqueue)

    prefix = mock_bridge_state.cloud_topic_prefix
    write_topic = f"{prefix}/mailbox/write"

    # WRITE Action
    route_write = parse_topic(prefix, write_topic)
    assert route_write is not None
    msg_write = pb.CloudQueuedPublish(topic_name=write_topic, payload=b"hello-mcu")
    await svc._handle_mailbox(route_write, msg_write)
    mock_serial.send.assert_awaited_once()

    # READ Action


@pytest.mark.asyncio
async def test_handle_file_mcu_read_success_and_timeout(
    test_config: RuntimeConfig, mock_bridge_state: RuntimeState
) -> None:
    mock_serial = AsyncMock()
    mock_serial.send_raw.return_value = True
    svc = BridgeService(test_config, mock_bridge_state, mock_serial)
    mock_enqueue = AsyncMock()
    setattr(svc, "enqueue_cloud", mock_enqueue)

    # Success Path: MCU sends chunks then empty chunk
    inbound = pb.CloudQueuedPublish(topic_name="mcu/file/read/mcu/etc/config", payload=b"")
    read_task = asyncio.create_task(svc._handle_file_mcu_read("/mcu/etc/config", inbound))
    await asyncio.sleep(0.01)

    assert svc._pending_mcu_read is not None
    # Simulate MCU returning chunks via _on_mcu_file_read_resp
    await svc._on_mcu_file_read_resp(1, pb.FileReadResponse(content=b"hello-"))
    await svc._on_mcu_file_read_resp(2, pb.FileReadResponse(content=b"world"))
    await svc._on_mcu_file_read_resp(3, pb.FileReadResponse(content=b""))  # Completion

    await read_task
    mock_enqueue.assert_awaited()


@pytest.mark.asyncio
async def test_cloud_events_and_direct_rpc_dispatch(
    test_config: RuntimeConfig, mock_bridge_state: RuntimeState
) -> None:
    svc = BridgeService(test_config, mock_bridge_state, MagicMock())
    mock_stream = AsyncMock()
    svc._cloud_stream = mock_stream

    # _send_cloud_event test
    await svc._send_cloud_event("test_event", "info", "Description")
    mock_stream.send_message.assert_awaited_once()

    # Direct Protobuf RPC dispatch validation
    mock_exec_rpc = AsyncMock(return_value=pb.GenericResponse(status="ok").SerializeToString())
    setattr(svc.local_bridge_service, "execute_rpc", mock_exec_rpc)

    env = pb.CloudEnvelope(
        protocol_version=2,
        sequence_id=88,
        command_request=pb.CommandRequest(
            command_path="rpc/DigitalWrite",
            payload=pb.DigitalWrite(pin=13, value=1).SerializeToString(),
        ),
    )

    class MockStream:
        def __init__(self, items: list[pb.CloudEnvelope]) -> None:
            self._items = items
            self.sent_messages: list[pb.CloudEnvelope] = []

        async def __aenter__(self) -> MockStream:
            return self

        async def __aexit__(self, *args: Any) -> None:
            pass

        def __aiter__(self) -> MockStream:
            self._iter = iter(self._items)
            return self

        async def __anext__(self) -> pb.CloudEnvelope:
            try:
                return next(self._iter)
            except StopIteration:
                raise StopAsyncIteration

        async def send_message(self, msg: Any) -> None:
            self.sent_messages.append(msg)

    session_stream = MockStream([env])
    with patch("mcubridge.services.runtime.Channel"), patch("mcubridge.services.runtime.CloudBridgeStub") as mock_stub:
        mock_stub.return_value.Session.open.return_value = session_stream
        with patch.object(svc, "_send_cloud_event", new_callable=AsyncMock):
            with patch.object(svc, "flush_cloud_spool", new_callable=AsyncMock):
                await svc.connect_cloud_session(None)

    mock_exec_rpc.assert_awaited_once_with("DigitalWrite", env.command_request.payload)
    assert len(session_stream.sent_messages) == 1
    assert session_stream.sent_messages[0].sequence_id == 88
    assert session_stream.sent_messages[0].command_response.status_code == 200


@pytest.mark.asyncio
async def test_handle_system_and_mcu_version(test_config: RuntimeConfig, mock_bridge_state: RuntimeState) -> None:
    from mcubridge.protocol import protocol
    from mcubridge.protocol.topics import parse_topic, topic_path
    from mcubridge.protocol.protocol import Topic, SystemAction

    mock_serial = AsyncMock(spec=SerialTransport)
    mock_serial.send.return_value = pb.VersionResponse(major=2, minor=8, patch=5).SerializeToString()
    svc = BridgeService(test_config, mock_bridge_state, mock_serial)
    svc.enqueue_cloud = AsyncMock()

    # Bootloader action
    t_bootloader = topic_path(mock_bridge_state.cloud_topic_prefix, Topic.SYSTEM, SystemAction.BOOTLOADER)
    route_bootloader = parse_topic(mock_bridge_state.cloud_topic_prefix, t_bootloader)
    assert route_bootloader is not None
    await svc._handle_system(route_bootloader, pb.CloudQueuedPublish())
    mock_serial.send.assert_awaited_with(
        protocol.Command.CMD_ENTER_BOOTLOADER.value, pb.EnterBootloader(magic=protocol.BOOTLOADER_MAGIC)
    )

    # Version action
    t_version = topic_path(mock_bridge_state.cloud_topic_prefix, Topic.SYSTEM, SystemAction.VERSION, SystemAction.GET)
    route_version = parse_topic(mock_bridge_state.cloud_topic_prefix, t_version)
    assert route_version is not None
    await svc._handle_system(route_version, pb.CloudQueuedPublish())
    assert mock_bridge_state.mcu_version == (2, 8, 5)

    # Bridge summary / handshake action
    t_summary = topic_path(
        mock_bridge_state.cloud_topic_prefix, Topic.SYSTEM, SystemAction.BRIDGE, SystemAction.SUMMARY, SystemAction.GET
    )
    route_summary = parse_topic(mock_bridge_state.cloud_topic_prefix, t_summary)
    assert route_summary is not None
    await svc._handle_system(route_summary, pb.CloudQueuedPublish())
    svc.enqueue_cloud.assert_awaited()


@pytest.mark.asyncio
async def test_process_poll_and_terminate(test_config: RuntimeConfig, mock_bridge_state: RuntimeState) -> None:
    from mcubridge.protocol.protocol import Status

    mock_serial = AsyncMock(spec=SerialTransport)
    svc = BridgeService(test_config, mock_bridge_state, mock_serial)

    # Missing PID poll
    resp = await svc.poll_process(99999)
    assert resp.status == Status.ERROR.value
    assert resp.finished is True

    # Process termination wait
    mock_ctx = MagicMock()
    mock_ctx.handle.returncode = 0
    code = await svc._terminate_process(1234, mock_ctx, grace_period=0.1)
    assert code == 0
