"""Surgical unit test suite targeting uncovered branches in runtime.py."""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable
from typing import Any
from unittest.mock import AsyncMock, MagicMock

from google.protobuf.message import EncodeError as ProtobufSerializationError
import mcubridge.protocol.mcubridge_pb2 as pb
import pytest
from mcubridge.config.settings import RuntimeConfig
from mcubridge.protocol import protocol
from mcubridge.protocol.protocol import Status
from mcubridge.services.runtime import BridgeService
from mcubridge.state.context import RuntimeState, create_runtime_state
from mcubridge.transport.serial import SerialTransport
from pytest_mock import MockerFixture

from tools.testing.mock_stream import MockGrpcStream


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


async def test_enqueue_cloud_console_queue(test_config: RuntimeConfig, mock_bridge_state: RuntimeState) -> None:
    svc = BridgeService(test_config, mock_bridge_state, MagicMock())
    cq: asyncio.Queue[pb.CloudQueuedPublish] = asyncio.Queue()
    svc.console_queues.append(cq)

    msg = pb.CloudQueuedPublish(topic_name="mcu/console/stdout", payload=b"hello")
    svc.cloud_publisher = AsyncMock()
    await svc.enqueue_cloud(msg)

    pushed = await cq.get()
    assert pushed.payload == b"hello"


async def test_spool_cloud_message_trim_limit(test_config: RuntimeConfig, mock_bridge_state: RuntimeState) -> None:
    svc = BridgeService(test_config, mock_bridge_state, MagicMock())
    mock_bridge_state.cloud_queue_limit = 5
    mock_spool = MagicMock()
    mock_spool.append = AsyncMock(return_value=1)
    mock_spool.__len__.return_value = 5
    svc.cloud_spool = mock_spool

    msg = pb.CloudQueuedPublish(topic_name="mcu/test", payload=b"data")
    spool_fn: Callable[[pb.CloudQueuedPublish], Awaitable[bool]] = svc.spool_cloud_message_locked
    res = await spool_fn(msg)

    assert res is True
    assert mock_bridge_state.cloud_spool_dropped_limit == 1
    assert mock_bridge_state.cloud_spool_trim_events == 1


async def test_spool_cloud_message_exceptions(test_config: RuntimeConfig, mock_bridge_state: RuntimeState) -> None:
    svc = BridgeService(test_config, mock_bridge_state, MagicMock())

    # Case 1: No spool
    svc.cloud_spool = None
    msg = pb.CloudQueuedPublish(topic_name="test", payload=b"a")
    spool_fn: Callable[[pb.CloudQueuedPublish], Awaitable[bool]] = svc.spool_cloud_message_locked
    assert await spool_fn(msg) is False

    # Case 2: Database error on append
    mock_spool = MagicMock()
    mock_spool.__len__.return_value = 0
    mock_spool.append = AsyncMock(side_effect=OSError("Disk full"))
    svc.cloud_spool = mock_spool
    assert await spool_fn(msg) is False
    assert mock_bridge_state.cloud_spool_degraded is True


async def test_flush_cloud_spool_corrupt_entry(test_config: RuntimeConfig, mock_bridge_state: RuntimeState) -> None:
    svc = BridgeService(test_config, mock_bridge_state, MagicMock())
    mock_spool = MagicMock()
    svc.cloud_stream = MagicMock()
    svc.cloud_spool = mock_spool

    # First peek returns garbage bytes, causing ProtobufDecodeError/ValueError
    mock_spool.__len__.side_effect = [2, 1, 0, 0]
    mock_spool.peek = AsyncMock(return_value=b"\xff\xff\xff\xff")
    mock_spool.popleft = AsyncMock(return_value=None)
    mock_spool.vacuum = AsyncMock(return_value=None)

    flush_spool: Callable[[], Awaitable[None]] = svc.flush_cloud_spool_locked
    await flush_spool()

    assert mock_bridge_state.cloud_spool_corrupt_dropped == 1
    mock_spool.vacuum.assert_awaited_once()


async def test_handle_mcu_status_formatting(test_config: RuntimeConfig, mock_bridge_state: RuntimeState) -> None:
    svc = BridgeService(test_config, mock_bridge_state, MagicMock())
    mock_enqueue = AsyncMock()
    svc.cloud_publisher = mock_enqueue

    # Test status with GenericResponse payload
    resp = pb.GenericResponse(message="System initialized")
    handle_status: Callable[..., Awaitable[None]] = svc.handle_mcu_status
    await handle_status(Status.OK, 1, resp)
    mock_enqueue.assert_awaited()

    # Test status with raw bytes payload
    await handle_status(Status.ERROR, 2, b"Raw error bytes")
    assert mock_enqueue.call_count == 2


async def test_handle_datastore_actions(test_config: RuntimeConfig, mock_bridge_state: RuntimeState) -> None:
    svc = BridgeService(test_config, mock_bridge_state, MagicMock())
    cache = AsyncMock()
    cache.get.return_value = b"cached-value"
    cache.set.return_value = None
    mock_bridge_state.datastore_cache = cache
    mock_publish = AsyncMock()
    svc.cloud_publisher = mock_publish

    prefix = mock_bridge_state.cloud_topic_prefix
    put_topic = f"{prefix}/req/datastore.put"
    get_topic = f"{prefix}/req/datastore.get"

    # PUT Action
    msg_put = pb.CloudQueuedPublish(
        topic_name=put_topic, payload=pb.DatastorePut(key="temp", value=b"25.4").SerializeToString()
    )
    await svc.handle_request(msg_put)
    cache.set.assert_awaited_with("temp", b"25.4")

    # GET Action (Cache Hit)
    msg_get = pb.CloudQueuedPublish(topic_name=get_topic, payload=pb.DatastoreGet(key="temp").SerializeToString())
    await svc.handle_request(msg_get)
    assert mock_publish.await_count >= 1


async def test_handle_mailbox_read_write(test_config: RuntimeConfig, mock_bridge_state: RuntimeState) -> None:
    mock_serial = AsyncMock()
    mock_serial.send.return_value = True
    svc = BridgeService(test_config, mock_bridge_state, mock_serial)
    mock_enqueue = AsyncMock()
    svc.cloud_publisher = mock_enqueue

    prefix = mock_bridge_state.cloud_topic_prefix
    write_topic = f"{prefix}/req/mailbox.push"

    # WRITE Action
    msg_write = pb.CloudQueuedPublish(
        topic_name=write_topic, payload=pb.MailboxPush(data=b"hello-mcu").SerializeToString()
    )
    await svc.handle_request(msg_write)
    mock_serial.send.assert_awaited_once()


async def test_handle_file_mcu_read_success_and_timeout(
    test_config: RuntimeConfig, mock_bridge_state: RuntimeState
) -> None:
    mock_serial = AsyncMock()
    mock_serial.send_raw.return_value = True
    svc = BridgeService(test_config, mock_bridge_state, mock_serial)
    mock_enqueue = AsyncMock()
    svc.cloud_publisher = mock_enqueue

    # Success Path: MCU sends chunks then empty chunk
    inbound = pb.CloudQueuedPublish(
        topic_name=f"{mock_bridge_state.cloud_topic_prefix}/req/file.read",
        payload=pb.FileRead(path="/mcu/etc/config").SerializeToString(),
    )
    read_task: asyncio.Task[None] = asyncio.create_task(svc.handle_request(inbound))
    await asyncio.sleep(0.01)

    assert svc.pending_mcu_read is not None
    # Simulate MCU returning chunks via on_mcu_file_read_resp
    on_read_resp: Callable[..., Awaitable[bool]] = svc.on_mcu_file_read_resp
    await on_read_resp(1, pb.FileReadResponse(content=b"hello-"))
    await on_read_resp(2, pb.FileReadResponse(content=b"world"))
    await on_read_resp(3, pb.FileReadResponse(content=b""))  # Completion

    await read_task
    mock_enqueue.assert_awaited()
    assert mock_enqueue.await_args is not None
    resp_msg: pb.CloudQueuedPublish = mock_enqueue.await_args[0][0]
    resp = pb.FileReadResponse.FromString(resp_msg.payload)
    assert resp.content == b"hello-world"


async def test_cloud_events_and_direct_rpc_dispatch(
    test_config: RuntimeConfig, mock_bridge_state: RuntimeState, mocker: MockerFixture
) -> None:
    mock_serial = AsyncMock(spec=SerialTransport)
    mock_serial.send = AsyncMock(return_value=pb.GenericResponse(status="ok").SerializeToString())
    svc = BridgeService(test_config, mock_bridge_state, mock_serial)
    mock_stream = MockGrpcStream[Any, pb.CloudEnvelope]()
    svc.cloud_stream = mock_stream

    # _send_cloud_event test
    send_event: Callable[..., Awaitable[None]] = svc.send_cloud_event
    await send_event("test_event", "info", "Description")
    mock_stream.send_message.assert_awaited_once()
    assert mock_stream.sent_message.event.event_type == "test_event"

    env = pb.CloudEnvelope(
        protocol_version=protocol.PROTOCOL_VERSION,
        sequence_id=88,
        command_request=pb.CommandRequest(
            command_path="rpc/DigitalWrite",
            payload=pb.DigitalWrite(pin=13, value=1).SerializeToString(),
        ),
    )

    session_stream = MockGrpcStream[pb.CloudEnvelope, pb.CloudEnvelope](inbound=[env])
    mocker.patch("mcubridge.services.runtime.Channel")
    mock_stub = mocker.patch("mcubridge.services.runtime.CloudBridgeStub")
    mock_stub.return_value.Session.open.return_value = session_stream

    await svc.connect_cloud_session(None)

    mock_serial.send.assert_awaited_once()
    assert len(session_stream.sent_messages) == 2
    assert session_stream.sent_messages[0].event.event_type == "status_online"
    assert session_stream.sent_messages[1].sequence_id == 88
    assert session_stream.sent_messages[1].command_response.status_code == 200


async def test_handle_system_and_mcu_version(test_config: RuntimeConfig, mock_bridge_state: RuntimeState) -> None:
    from mcubridge.protocol import protocol

    mock_serial = AsyncMock(spec=SerialTransport)
    mock_serial.send.return_value = pb.VersionResponse(major=2, minor=8, patch=5).SerializeToString()
    svc = BridgeService(test_config, mock_bridge_state, mock_serial)
    mock_publisher = AsyncMock()
    svc.cloud_publisher = mock_publisher

    # Bootloader action
    t_bootloader = f"{mock_bridge_state.cloud_topic_prefix}/req/system.bootloader"
    await svc.handle_request(pb.CloudQueuedPublish(topic_name=t_bootloader, payload=b""))
    mock_serial.send.assert_awaited_with(
        protocol.Command.CMD_ENTER_BOOTLOADER.value, pb.EnterBootloader(magic=protocol.BOOTLOADER_MAGIC)
    )

    # Version action
    t_version = f"{mock_bridge_state.cloud_topic_prefix}/req/system.version"
    await svc.handle_request(pb.CloudQueuedPublish(topic_name=t_version, payload=b""))
    assert mock_bridge_state.mcu_version == (2, 8, 5)

    # System status action
    t_status = f"{mock_bridge_state.cloud_topic_prefix}/req/system.status"
    await svc.handle_request(pb.CloudQueuedPublish(topic_name=t_status, payload=b""))
    mock_publisher.assert_awaited()


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
    term_proc: Callable[..., Awaitable[int]] = svc.terminate_process
    code = await term_proc(1234, mock_ctx, grace_period=0.1)
    assert code == 0


async def test_enqueue_cloud_not_debug_and_drop(test_config: RuntimeConfig, mock_bridge_state: RuntimeState) -> None:
    test_config.cloud_enabled = True
    test_config.cloud_spool_dir = ""
    mock_serial = AsyncMock(spec=SerialTransport)
    svc = BridgeService(test_config, mock_bridge_state, mock_serial)
    logging.getLogger("mcubridge.service").setLevel(logging.INFO)

    while not mock_bridge_state.cloud_publish_queue.empty():
        mock_bridge_state.cloud_publish_queue.get_nowait()

    msg = pb.CloudQueuedPublish(topic_name="test/topic", payload=b"payload")
    await svc.enqueue_cloud(msg)
    assert mock_bridge_state.cloud_drop_counts.get("test/topic") == 1


async def test_flush_cloud_spool_publish_fails_and_degraded(
    test_config: RuntimeConfig, mock_bridge_state: RuntimeState
) -> None:
    mock_serial = AsyncMock(spec=SerialTransport)
    svc = BridgeService(test_config, mock_bridge_state, mock_serial)
    mock_stream = AsyncMock()
    mock_stream.send_message.side_effect = OSError("Connection error")
    svc.cloud_stream = mock_stream

    spool = svc.cloud_spool
    assert spool is not None
    await spool.append(pb.CloudQueuedPublish(topic_name="spool/topic", payload=b"data").SerializeToString())

    # 1. _publish_cloud_message returns False when send_message raises OSError -> triggers break
    flush_spool: Callable[[], Awaitable[None]] = svc.flush_cloud_spool_locked
    await flush_spool()
    assert len(spool) == 1

    mock_bridge_state.cloud_spool_degraded = False
    mock_bridge_state.cloud_spool_failure_reason = None
    mock_stream.send_message.side_effect = None
    failing_peek = AsyncMock(side_effect=OSError("simulated_error"))
    spool.peek = failing_peek
    await flush_spool()
    assert mock_bridge_state.cloud_spool_degraded
    assert mock_bridge_state.cloud_spool_failure_reason == "simulated_error"


async def test_enqueue_cloud_disabled_noop(test_config: RuntimeConfig, mock_bridge_state: RuntimeState) -> None:
    test_config.cloud_enabled = False
    mock_serial = AsyncMock(spec=SerialTransport)
    svc = BridgeService(test_config, mock_bridge_state, mock_serial)
    msg = pb.CloudQueuedPublish(topic_name="mcu/data", payload=b"test")
    await svc.enqueue_cloud(msg)
    assert mock_bridge_state.cloud_publish_queue.empty()


async def test_enqueue_cloud_publish_success(test_config: RuntimeConfig, mock_bridge_state: RuntimeState) -> None:
    test_config.cloud_enabled = True
    mock_serial = AsyncMock(spec=SerialTransport)
    svc = BridgeService(test_config, mock_bridge_state, mock_serial)
    svc.publish_cloud_message = AsyncMock(return_value=True)
    msg = pb.CloudQueuedPublish(topic_name="mcu/data", payload=b"test")
    await svc.enqueue_cloud(msg)
    assert mock_bridge_state.cloud_publish_queue.empty()


async def test_spool_cloud_message_serialization_error(
    test_config: RuntimeConfig, mock_bridge_state: RuntimeState
) -> None:
    mock_serial = AsyncMock(spec=SerialTransport)
    svc = BridgeService(test_config, mock_bridge_state, mock_serial)
    bad_msg = MagicMock(spec=pb.CloudQueuedPublish)
    bad_msg.SerializeToString.side_effect = ProtobufSerializationError("serialization_fail")
    res = await svc.spool_cloud_message_locked(bad_msg)
    assert res is False


async def test_publish_cloud_message_debug_logging(test_config: RuntimeConfig, mock_bridge_state: RuntimeState) -> None:
    mock_serial = AsyncMock(spec=SerialTransport)
    svc = BridgeService(test_config, mock_bridge_state, mock_serial)
    svc.cloud_stream = AsyncMock()
    logging.getLogger("mcubridge.service").setLevel(logging.DEBUG)
    msg = pb.CloudQueuedPublish(topic_name="mcu/telemetry", payload=b"test")
    success = await svc.publish_cloud_message(msg)
    assert success is True


def test_bridge_service_lifecycle_fsm_properties(test_config: RuntimeConfig, mock_bridge_state: RuntimeState) -> None:
    mock_serial = AsyncMock(spec=SerialTransport)
    svc = BridgeService(test_config, mock_bridge_state, mock_serial)
    assert not svc.is_running
    assert not svc.is_draining
    svc.lifecycle_fsm.start()
    svc.lifecycle_fsm.run()
    assert svc.is_running
    assert not svc.is_draining
    svc.lifecycle_fsm.drain()
    assert not svc.is_running
    assert svc.is_draining
    svc.lifecycle_fsm.stop()
    assert not svc.is_running
    assert not svc.is_draining


async def test_reset_link_branches(test_config: RuntimeConfig, mock_bridge_state: RuntimeState) -> None:
    mock_serial = AsyncMock(spec=SerialTransport)
    svc = BridgeService(test_config, mock_bridge_state, mock_serial)
    assert await svc.reset_link() is True
    mock_serial.reset.assert_awaited_once()

    svc.serial = None
    assert await svc.reset_link() is False


async def test_request_mcu_version_branches(test_config: RuntimeConfig, mock_bridge_state: RuntimeState) -> None:
    mock_serial = AsyncMock(spec=SerialTransport)
    svc = BridgeService(test_config, mock_bridge_state, mock_serial)

    svc.serial = None
    assert await svc.request_mcu_version() is False

    svc.serial = mock_serial
    mock_serial.send.return_value = b"\xff\xff\xff"
    assert await svc.request_mcu_version() is False


async def test_flush_console_queue_without_serial(test_config: RuntimeConfig, mock_bridge_state: RuntimeState) -> None:
    mock_serial = AsyncMock(spec=SerialTransport)
    svc = BridgeService(test_config, mock_bridge_state, mock_serial)
    svc.serial = None
    await svc.flush_console_queue()
    assert len(mock_bridge_state.console_to_mcu_queue) == 0


async def test_write_with_quota_exceeds_max_bytes(
    test_config: RuntimeConfig, mock_bridge_state: RuntimeState, tmp_path: Path
) -> None:
    mock_serial = AsyncMock(spec=SerialTransport)
    svc = BridgeService(test_config, mock_bridge_state, mock_serial)
    mock_bridge_state.file_write_max_bytes = 10
    target_file = tmp_path / "quota_exceeded.txt"
    res = await svc.write_with_quota(target_file, b"0123456789abcdef")
    assert res is False
    assert mock_bridge_state.file_write_limit_rejections == 1


async def test_on_mcu_process_kill_and_subscribe_resp(
    test_config: RuntimeConfig, mock_bridge_state: RuntimeState
) -> None:
    mock_serial = AsyncMock(spec=SerialTransport)
    svc = BridgeService(test_config, mock_bridge_state, mock_serial)
    svc.kill_process = AsyncMock(return_value=True)
    await svc.on_mcu_process_kill(1, pb.ProcessKill(pid=42))
    svc.kill_process.assert_awaited_once_with(42)

    await svc.on_mcu_pin_subscribe_resp(1, pb.PinSubscribeResponse(pin=13, success=True))


async def test_enqueue_cloud_queue_empty_in_finally(
    test_config: RuntimeConfig, mock_bridge_state: RuntimeState
) -> None:
    test_config.cloud_enabled = True
    mock_serial = AsyncMock(spec=SerialTransport)
    svc = BridgeService(test_config, mock_bridge_state, mock_serial)

    async def mock_spool(msg: Any) -> bool:
        try:
            mock_bridge_state.cloud_publish_queue.get_nowait()
        except asyncio.QueueEmpty as exc:
            logging.debug("Queue already empty during mock spool", exc_info=exc)
        return True

    svc.spool_cloud_message_locked = mock_spool
    msg = pb.CloudQueuedPublish(topic_name="mcu/data", payload=b"test")
    await svc.enqueue_cloud(msg)
    assert mock_bridge_state.cloud_publish_queue.empty()


async def test_handle_request_without_serial(test_config: RuntimeConfig, mock_bridge_state: RuntimeState) -> None:
    mock_serial = AsyncMock(spec=SerialTransport)
    svc = BridgeService(test_config, mock_bridge_state, mock_serial)
    svc.serial = None
    msg = pb.CloudQueuedPublish(topic_name="br/d/13/write", payload=b"1")
    await svc.handle_request(msg)
    assert svc.serial is None
    assert len(mock_bridge_state.running_processes) == 0


async def test_on_mcu_console_write_no_queues(test_config: RuntimeConfig, mock_bridge_state: RuntimeState) -> None:
    mock_serial = AsyncMock(spec=SerialTransport)
    svc = BridgeService(test_config, mock_bridge_state, mock_serial)
    svc.console_queues.clear()
    await svc.on_mcu_console_write(1, pb.ConsoleWrite(data=b"hello mcu"))
    assert len(svc.console_queues) == 0
    assert len(mock_bridge_state.console_to_mcu_queue) == 0


async def test_run_cloud_disabled(test_config: RuntimeConfig, mock_bridge_state: RuntimeState) -> None:
    test_config.cloud_enabled = False
    mock_serial = AsyncMock(spec=SerialTransport)
    svc = BridgeService(test_config, mock_bridge_state, mock_serial)
    await svc.run_cloud()
    assert mock_bridge_state.cloud_fsm.disabled.is_active


