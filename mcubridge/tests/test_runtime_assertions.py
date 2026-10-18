"""Assertive, deterministic tests for McuBridge runtime service."""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import cast
from unittest.mock import AsyncMock

import pytest
from mcubridge.config.settings import RuntimeConfig
from mcubridge.protocol import mcubridge_pb2 as pb
from mcubridge.protocol.protocol import Command
from mcubridge.services.runtime import BridgeService
from mcubridge.state.context import RuntimeState, create_runtime_state
from mcubridge.transport.serial import SerialTransport


class QoS:
    def __init__(self, value: int) -> None:
        self.value = value


class PublishPacket:
    def __init__(
        self,
        topic: str,
        payload: bytes,
        qos: QoS,
        retain: bool = False,
        message_expiry_interval: int | None = None,
        content_type: str | None = None,
        response_topic: str | None = None,
        correlation_data: bytes | None = None,
        user_properties: list[tuple[str, str]] | None = None,
    ) -> None:
        self.topic = topic
        self.payload = payload
        self.qos = qos
        self.retain = retain
        self.message_expiry_interval = message_expiry_interval
        self.content_type = content_type
        self.response_topic = response_topic
        self.correlation_data = correlation_data
        self.user_properties = user_properties or []


@pytest.fixture
def service_setup(tmp_path: Path) -> tuple[BridgeService, RuntimeState, AsyncMock]:
    cfg = RuntimeConfig(
        topic_prefix="br",
        serial_port="/dev/null",
        serial_baud=115200,
        serial_safe_baud=9600,
        allowed_commands=["ls", "echo", "sleep"],
        file_system_root=str(tmp_path),
        allow_non_tmp_paths=True,
    )
    state = create_runtime_state(cfg)
    state.file_system_root = str(tmp_path)
    state.allow_non_tmp_paths = True
    state.connection_fsm.connect()
    state.connection_fsm.synchronize()

    mock_serial = AsyncMock(spec=SerialTransport)
    mock_cloud_publisher = AsyncMock()
    service = BridgeService(cfg, state, mock_serial, cloud_publisher=mock_cloud_publisher)
    return service, state, mock_serial


async def test_mcu_file_read_handler_asserts_state(
    service_setup: tuple[BridgeService, RuntimeState, AsyncMock],
) -> None:
    service, _state, mock_serial = service_setup
    (Path(service.config.file_system_root) / "test.txt").write_bytes(b"file_data")
    payload = pb.FileRead(path="test.txt").SerializeToString()

    await service.handle_mcu_frame(Command.CMD_FILE_READ.value, 1, payload)
    mock_serial.send.assert_awaited()
    args = mock_serial.send.call_args[0]
    assert args[0] == Command.CMD_FILE_READ_RESP.value
    assert isinstance(args[1], pb.FileReadResponse)
    assert args[1].content == b"file_data"


async def test_cloud_file_write_asserts_serial(
    service_setup: tuple[BridgeService, RuntimeState, AsyncMock],
) -> None:
    service, state, serial = service_setup
    serial.send.return_value = True
    msg = pb.CloudQueuedPublish(
        topic_name=f"{state.cloud_topic_prefix}/req/file.write",
        payload=pb.FileWrite(path="mcu/test.txt", data=b"new_data").SerializeToString(),
    )

    await service.handle_request(msg)

    serial.send.assert_called_once()
    assert serial.send.call_args[0][0] == Command.CMD_FILE_WRITE.value
    assert isinstance(serial.send.call_args[0][1], pb.FileWrite)
    assert serial.send.call_args[0][1].data == b"new_data"


async def test_cloud_datastore_put_asserts_cache(
    service_setup: tuple[BridgeService, RuntimeState, AsyncMock],
) -> None:
    service, state, _serial = service_setup
    msg = pb.CloudQueuedPublish(
        topic_name=f"{state.cloud_topic_prefix}/req/datastore.put",
        payload=pb.DatastorePut(key="my_key", value=b"my_value").SerializeToString(),
    )

    await service.handle_request(msg)

    assert state.datastore_cache is not None
    assert await state.datastore_cache.get("my_key") == b"my_value"


async def test_mcu_datastore_put_asserts_cloud(
    service_setup: tuple[BridgeService, RuntimeState, AsyncMock],
) -> None:
    service, state, _serial = service_setup
    payload = pb.DatastorePut(key="mcu_key", value=b"mcu_val").SerializeToString()

    await service.handle_mcu_frame(Command.CMD_DATASTORE_PUT.value, 1, payload)

    assert state.datastore_cache is not None
    assert await state.datastore_cache.get("mcu_key") == b"mcu_val"

    mock_pub = cast(AsyncMock, service.cloud_publisher)
    mock_pub.assert_called_once()
    queued_pub = cast(pb.CloudQueuedPublish, mock_pub.call_args[0][0])
    assert queued_pub.topic_name == f"{state.cloud_topic_prefix}/res/datastore.get"
    assert queued_pub.payload == b"mcu_val"


async def test_local_bridge_datastore_put_persists_and_publishes(
    service_setup: tuple[BridgeService, RuntimeState, AsyncMock],
) -> None:
    service, state, _serial = service_setup
    request = pb.DatastorePut(key="local_key", value=b"local_value")

    response = await service.local_bridge_service.execute_datastore_put(request)

    assert response.status == "ok"
    assert state.datastore_cache is not None
    assert await state.datastore_cache.get("local_key") == b"local_value"
    mock_pub = cast(AsyncMock, service.cloud_publisher)
    mock_pub.assert_awaited_once()
    publish_call = mock_pub.await_args
    assert publish_call is not None
    queued_pub = cast(pb.CloudQueuedPublish, publish_call.args[0])
    assert queued_pub.topic_name == f"{state.cloud_topic_prefix}/res/datastore.get"
    assert queued_pub.payload == b"local_value"


async def test_mcu_mailbox_push_asserts_cloud(
    service_setup: tuple[BridgeService, RuntimeState, AsyncMock],
) -> None:
    service, state, _serial = service_setup
    payload = pb.MailboxPush(data=b"pushed_msg").SerializeToString()

    await service.handle_mcu_frame(Command.CMD_MAILBOX_PUSH.value, 1, payload)

    mock_pub = cast(AsyncMock, service.cloud_publisher)
    mock_pub.assert_called_once()
    queued_pub = cast(pb.CloudQueuedPublish, mock_pub.call_args[0][0])
    assert queued_pub.topic_name == f"{state.cloud_topic_prefix}/mailbox/incoming"
    assert queued_pub.payload == b"pushed_msg"


async def test_cloud_mailbox_write_asserts_serial(
    service_setup: tuple[BridgeService, RuntimeState, AsyncMock],
) -> None:
    service, state, serial = service_setup
    serial.send.return_value = True
    msg = pb.CloudQueuedPublish(
        topic_name=f"{state.cloud_topic_prefix}/req/mailbox.push",
        payload=pb.MailboxPush(data=b"outbound_box").SerializeToString(),
    )

    await service.handle_request(msg)

    serial.send.assert_called_once()
    assert serial.send.call_args[0][0] == Command.CMD_MAILBOX_PUSH.value
    assert isinstance(serial.send.call_args[0][1], pb.MailboxPush)
    assert serial.send.call_args[0][1].data == b"outbound_box"


async def test_mcu_process_run_asserts_exec(
    service_setup: tuple[BridgeService, RuntimeState, AsyncMock],
) -> None:
    service, _state, serial = service_setup
    serial.send.return_value = True
    payload = pb.ProcessRunAsync(command="echo hello").SerializeToString()

    await service.handle_mcu_frame(Command.CMD_PROCESS_RUN_ASYNC.value, 1, payload)

    serial.send.assert_awaited()
    args = serial.send.call_args[0]
    assert args[0] == Command.CMD_PROCESS_RUN_ASYNC_RESP.value
    assert isinstance(args[1], pb.ProcessRunAsyncResponse)
    pid = args[1].pid
    assert pid > 0
    await service.kill_process(pid)


async def test_cloud_spi_transfer_asserts_serial(
    service_setup: tuple[BridgeService, RuntimeState, AsyncMock],
) -> None:
    service, state, serial = service_setup
    serial.send.return_value = True
    msg = pb.CloudQueuedPublish(
        topic_name=f"{state.cloud_topic_prefix}/req/spi.transfer",
        payload=pb.SpiTransfer(data=b"\x01\x02\x03").SerializeToString(),
    )

    await service.handle_request(msg)

    serial.send.assert_called_once()
    assert serial.send.call_args[0][0] == Command.CMD_SPI_TRANSFER.value
    assert isinstance(serial.send.call_args[0][1], pb.SpiTransfer)
    assert serial.send.call_args[0][1].data == b"\x01\x02\x03"


async def test_cloud_file_host_write_asserts_cache(
    service_setup: tuple[BridgeService, RuntimeState, AsyncMock],
) -> None:
    service, state, _serial = service_setup
    msg = pb.CloudQueuedPublish(
        topic_name=f"{state.cloud_topic_prefix}/req/file.write",
        payload=pb.FileWrite(path="test.txt", data=b"host_file_payload").SerializeToString(),
    )

    await service.handle_request(msg)

    assert (Path(service.config.file_system_root) / "test.txt").read_bytes() == b"host_file_payload"
    mock_pub = cast(AsyncMock, service.cloud_publisher)
    mock_pub.assert_called_once()
    queued_pub = cast(pb.CloudQueuedPublish, mock_pub.call_args[0][0])
    assert queued_pub.topic_name == f"{state.cloud_topic_prefix}/res/file.write"
    resp = pb.GenericResponse.FromString(queued_pub.payload)
    assert resp.status == "ok"


async def test_cloud_file_host_read_asserts_read(
    service_setup: tuple[BridgeService, RuntimeState, AsyncMock],
) -> None:
    service, state, _serial = service_setup
    (Path(service.config.file_system_root) / "test.txt").write_bytes(b"disk_data")
    msg = pb.CloudQueuedPublish(
        topic_name=f"{state.cloud_topic_prefix}/req/file.read",
        payload=pb.FileRead(path="test.txt").SerializeToString(),
    )

    await service.handle_request(msg)

    mock_pub = cast(AsyncMock, service.cloud_publisher)
    mock_pub.assert_called_once()
    queued_pub = cast(pb.CloudQueuedPublish, mock_pub.call_args[0][0])
    assert queued_pub.topic_name == f"{state.cloud_topic_prefix}/res/file.read"
    resp = pb.FileReadResponse.FromString(queued_pub.payload)
    assert resp.content == b"disk_data"


async def test_cloud_shell_poll_asserts_cloud(
    service_setup: tuple[BridgeService, RuntimeState, AsyncMock],
) -> None:
    service, state, _serial = service_setup
    pid = await service.run_process("echo out")
    assert pid > 0
    await asyncio.sleep(0.05)

    msg = pb.CloudQueuedPublish(
        topic_name=f"{state.cloud_topic_prefix}/req/process.poll",
        payload=pb.ProcessPoll(pid=pid).SerializeToString(),
    )
    await service.handle_request(msg)

    mock_pub = cast(AsyncMock, service.cloud_publisher)
    mock_pub.assert_called_once()
    queued_pub = cast(pb.CloudQueuedPublish, mock_pub.call_args[0][0])
    assert queued_pub.topic_name == f"{state.cloud_topic_prefix}/res/process.poll"
    resp = pb.ProcessPollResponse.FromString(queued_pub.payload)
    assert resp.exit_code == 0
    assert resp.stdout_data == b"out\n"


async def test_cloud_shell_kill_asserts_cloud(
    service_setup: tuple[BridgeService, RuntimeState, AsyncMock],
) -> None:
    service, state, _serial = service_setup
    pid = await service.run_process("sleep 10")
    assert pid > 0
    assert pid in state.running_processes

    msg = pb.CloudQueuedPublish(
        topic_name=f"{state.cloud_topic_prefix}/req/process.kill",
        payload=pb.ProcessKill(pid=pid).SerializeToString(),
    )
    await service.handle_request(msg)

    assert pid not in state.running_processes


async def test_cloud_shell_run_asserts_exec(
    service_setup: tuple[BridgeService, RuntimeState, AsyncMock],
) -> None:
    service, state, _serial = service_setup
    msg = pb.CloudQueuedPublish(
        topic_name=f"{state.cloud_topic_prefix}/req/process.run_async",
        payload=pb.ProcessRunAsync(command="echo hello").SerializeToString(),
    )
    await service.handle_request(msg)

    mock_pub = cast(AsyncMock, service.cloud_publisher)
    mock_pub.assert_called_once()
    queued_pub = cast(pb.CloudQueuedPublish, mock_pub.call_args[0][0])
    assert queued_pub.topic_name == f"{state.cloud_topic_prefix}/res/process.run_async"
    resp = pb.ProcessRunAsyncResponse.FromString(queued_pub.payload)
    assert resp.pid > 0
    await service.kill_process(resp.pid)
