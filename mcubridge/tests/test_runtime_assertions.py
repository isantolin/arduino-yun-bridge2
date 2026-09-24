"""Assertive, deterministic tests for McuBridge runtime service."""

from __future__ import annotations

from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest
from pytest_mock import MockerFixture

from mcubridge.config.settings import RuntimeConfig
from mcubridge.protocol import mcubridge_pb2 as pb
from mcubridge.protocol.protocol import Command, Status
from mcubridge.services.runtime import BridgeService
from mcubridge.state.context import RuntimeState
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
def service_setup(tmp_path: object) -> tuple[BridgeService, RuntimeState, AsyncMock]:
    cfg = RuntimeConfig(
        serial_port="/dev/null",
        serial_baud=115200,
        serial_safe_baud=9600,
        allowed_commands=["ls", "echo"],
        file_system_root=str(tmp_path),
        allow_non_tmp_paths=True,
    )
    state = RuntimeState()
    state.file_system_root = str(tmp_path)
    state.allow_non_tmp_paths = True
    state.connection_fsm.connect()
    state.connection_fsm.synchronize()

    mock_serial = AsyncMock(spec=SerialTransport)
    service = BridgeService(cfg, state, mock_serial)
    service.enqueue_cloud = AsyncMock()  # type: ignore[method-assign]
    return service, state, mock_serial


@pytest.mark.asyncio
async def test_mcu_file_read_handler_asserts_state(
    service_setup: tuple[BridgeService, RuntimeState, AsyncMock], mocker: MockerFixture
) -> None:
    service, _state, _serial = service_setup
    payload = pb.FileRead(path="test.txt").SerializeToString()

    mocker.patch("pathlib.Path.read_bytes", return_value=b"file_data")
    mocker.patch("pathlib.Path.is_file", return_value=True)

    await service.handle_mcu_frame(Command.CMD_FILE_READ.value, 1, payload)
    service.serial.send.assert_awaited()  # type: ignore[union-attr]
    args = service.serial.send.call_args[0]  # type: ignore[union-attr]
    assert args[0] == Command.CMD_FILE_READ_RESP.value
    assert isinstance(args[1], pb.FileReadResponse)
    assert args[1].content == b"file_data"


@pytest.mark.asyncio
async def test_cloud_file_write_asserts_serial(
    service_setup: tuple[BridgeService, RuntimeState, AsyncMock],
) -> None:
    service, _state, serial = service_setup
    msg = pb.CloudQueuedPublish(
        topic_name="mcu/fs/write/test.txt",
        payload=b"new_data",
    )

    await service.handle_request(msg)

    serial.send.assert_called_once()
    assert serial.send.call_args[0][0] == Command.CMD_FILE_WRITE.value
    assert isinstance(serial.send.call_args[0][1], pb.FileWrite)
    assert serial.send.call_args[0][1].data == b"new_data"


@pytest.mark.asyncio
async def test_cloud_datastore_put_asserts_cache(
    service_setup: tuple[BridgeService, RuntimeState, AsyncMock],
) -> None:
    service, state, _serial = service_setup
    ds_put = pb.DatastorePut(key="my_key", value=b"my_value")
    msg = pb.CloudQueuedPublish(
        topic_name="mcu/ds/put",
        payload=ds_put.SerializeToString(),
    )

    await service.handle_request(msg)

    assert state.datastore_cache is not None
    assert await state.datastore_cache.get("my_key") == b"my_value"


@pytest.mark.asyncio
async def test_mcu_datastore_put_asserts_cloud(
    service_setup: tuple[BridgeService, RuntimeState, AsyncMock],
) -> None:
    service, state, _serial = service_setup
    payload = pb.DatastorePut(key="mcu_key", value=b"mcu_val").SerializeToString()

    await service.handle_mcu_frame(Command.CMD_DATASTORE_PUT.value, 1, payload)

    assert state.datastore_cache is not None
    assert await state.datastore_cache.get("mcu_key") == b"mcu_val"

    service.enqueue_cloud.assert_called_once()
    queued_pub = service.enqueue_cloud.call_args[0][0]
    assert queued_pub.topic_name == "mcu/datastore/mcu_key"
    assert queued_pub.payload == b"mcu_val"


@pytest.mark.asyncio
async def test_mcu_mailbox_push_asserts_cloud(
    service_setup: tuple[BridgeService, RuntimeState, AsyncMock],
) -> None:
    service, _state, _serial = service_setup
    payload = pb.MailboxPush(data=b"pushed_msg").SerializeToString()

    await service.handle_mcu_frame(Command.CMD_MAILBOX_PUSH.value, 1, payload)

    service.enqueue_cloud.assert_called_once()
    queued_pub = service.enqueue_cloud.call_args[0][0]
    assert queued_pub.topic_name == "mcu/mailbox"
    assert queued_pub.payload == b"pushed_msg"


@pytest.mark.asyncio
async def test_cloud_mailbox_write_asserts_serial(
    service_setup: tuple[BridgeService, RuntimeState, AsyncMock],
) -> None:
    service, _state, serial = service_setup
    msg = pb.CloudQueuedPublish(
        topic_name="mcu/mailbox/write",
        payload=b"outbound_box",
    )

    await service.handle_request(msg)

    serial.send.assert_called_once()
    assert serial.send.call_args[0][0] == Command.CMD_MAILBOX_PUSH.value
    assert isinstance(serial.send.call_args[0][1], pb.MailboxPush)
    assert serial.send.call_args[0][1].data == b"outbound_box"


@pytest.mark.asyncio
async def test_mcu_process_run_asserts_exec(
    service_setup: tuple[BridgeService, RuntimeState, AsyncMock], mocker: MockerFixture
) -> None:
    service, _state, serial = service_setup
    serial.send.return_value = True
    payload = pb.ProcessRunAsync(command="echo hello").SerializeToString()

    mocker.patch("mcubridge.services.runtime.is_command_allowed", return_value=True)
    mock_run = mocker.patch.object(service, "run_process", new=AsyncMock(return_value=1234))

    await service.handle_mcu_frame(Command.CMD_PROCESS_RUN_ASYNC.value, 1, payload)

    mock_run.assert_called_once_with("echo hello")
    serial.send.assert_awaited()
    args = serial.send.call_args[0]
    assert args[0] == Command.CMD_PROCESS_RUN_ASYNC_RESP.value
    assert isinstance(args[1], pb.ProcessRunAsyncResponse)
    assert args[1].pid == 1234


@pytest.mark.asyncio
async def test_cloud_spi_transfer_asserts_serial(
    service_setup: tuple[BridgeService, RuntimeState, AsyncMock],
) -> None:
    service, _state, serial = service_setup
    spi_req = pb.SpiTransfer(data=b"\x01\x02\x03")
    msg = pb.CloudQueuedPublish(
        topic_name="mcu/spi/transfer",
        payload=spi_req.SerializeToString(),
    )

    await service.handle_request(msg)

    serial.send.assert_called_once()
    assert serial.send.call_args[0][0] == Command.CMD_SPI_TRANSFER.value
    assert isinstance(serial.send.call_args[0][1], pb.SpiTransfer)
    assert serial.send.call_args[0][1].data == b"\x01\x02\x03"


@pytest.mark.asyncio
async def test_cloud_file_host_write_asserts_cache(
    service_setup: tuple[BridgeService, RuntimeState, AsyncMock], mocker: MockerFixture
) -> None:
    service, _state, _serial = service_setup
    msg = pb.CloudQueuedPublish(
        topic_name="bridge/file/write/test.txt",
        payload=b"host_file_payload",
    )

    mocker.patch("mcubridge.services.runtime.BridgeService.safe_file_write", return_value=True)
    await service.handle_request(msg)

    service.enqueue_cloud.assert_called_once()
    queued_pub = service.enqueue_cloud.call_args[0][0]
    assert queued_pub.topic_name == "mcu/file/read/test.txt"
    assert queued_pub.payload == b"host_file_payload"


@pytest.mark.asyncio
async def test_cloud_file_host_read_asserts_read(
    service_setup: tuple[BridgeService, RuntimeState, AsyncMock], mocker: MockerFixture
) -> None:
    service, _state, _serial = service_setup
    msg = pb.CloudQueuedPublish(
        topic_name="bridge/file/read/test.txt",
        payload=b"",
    )

    mocker.patch("pathlib.Path.is_file", return_value=True)
    mocker.patch("pathlib.Path.read_bytes", return_value=b"disk_data")

    await service.handle_request(msg)

    service.enqueue_cloud.assert_called_once()
    queued_pub = service.enqueue_cloud.call_args[0][0]
    assert queued_pub.topic_name == "mcu/file/read/test.txt/response"
    assert queued_pub.payload == b"disk_data"


@pytest.mark.asyncio
async def test_cloud_shell_poll_asserts_cloud(
    service_setup: tuple[BridgeService, RuntimeState, AsyncMock], mocker: MockerFixture
) -> None:
    service, _state, _serial = service_setup
    msg = pb.CloudQueuedPublish(
        topic_name="bridge/shell/poll/123",
        payload=b"",
    )

    mock_batch = pb.ProcessPollResponse(
        status=Status.OK.value,
        exit_code=0,
        finished=True,
        stdout=b"out",
        stderr=b"err",
        stdout_truncated=False,
        stderr_truncated=False,
    )
    mocker.patch("mcubridge.services.runtime.BridgeService.poll_process", return_value=mock_batch)
    await service.handle_request(msg)

    service.enqueue_cloud.assert_called_once()
    queued_pub = service.enqueue_cloud.call_args[0][0]
    assert queued_pub.topic_name == "mcu/shell/poll/123/response"
    resp = pb.ProcessPollResponse.FromString(queued_pub.payload)
    assert resp.exit_code == 0
    assert resp.stdout == b"out"


@pytest.mark.asyncio
async def test_cloud_shell_kill_asserts_cloud(
    service_setup: tuple[BridgeService, RuntimeState, AsyncMock], mocker: MockerFixture
) -> None:
    service, _state, _serial = service_setup
    msg = pb.CloudQueuedPublish(
        topic_name="bridge/shell/kill/123",
        payload=b"",
    )

    mocker.patch("mcubridge.services.runtime.is_command_allowed", return_value=True)
    mock_stop = mocker.patch("mcubridge.services.runtime.BridgeService.kill_process", return_value=(True, None))
    await service.handle_request(msg)

    mock_stop.assert_called_once_with(123)


@pytest.mark.asyncio
async def test_cloud_shell_run_asserts_exec(
    service_setup: tuple[BridgeService, RuntimeState, AsyncMock], mocker: MockerFixture
) -> None:
    service, _state, _serial = service_setup
    msg = pb.CloudQueuedPublish(
        topic_name="bridge/shell/run_async",
        payload=b"ls -la",
    )

    mocker.patch("mcubridge.services.runtime.is_command_allowed", return_value=True)
    mock_run = mocker.patch.object(service, "run_process", new=AsyncMock(return_value=999))
    await service.handle_request(msg)

    mock_run.assert_called_once_with("ls -la")
