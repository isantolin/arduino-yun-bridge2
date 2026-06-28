"""Assertive, deterministic tests for McuBridge runtime service."""

from __future__ import annotations
from unittest.mock import AsyncMock, patch

import pytest
import pytest_asyncio
from aiomqtt.message import Message

from mcubridge.protocol import mcubridge_pb2 as pb
from mcubridge.config.settings import RuntimeConfig
from mcubridge.protocol.protocol import Command
from mcubridge.services.runtime import BridgeService
from mcubridge.state.context import RuntimeState
from mcubridge.transport.serial import SerialTransport


@pytest_asyncio.fixture
async def service_setup(
    runtime_config: RuntimeConfig, runtime_state: RuntimeState
) -> tuple[BridgeService, RuntimeState, AsyncMock, AsyncMock]:
    serial = AsyncMock(spec=SerialTransport)
    serial.send.return_value = True
    serial.send_raw.return_value = True
    serial.acknowledge.return_value = True
    service = BridgeService(runtime_config, runtime_state, serial)
    mock_mqtt = AsyncMock()
    service.set_mqtt_client(mock_mqtt)
    return service, runtime_state, serial, mock_mqtt


@pytest.mark.asyncio
async def test_mcu_file_read_handler_asserts_state(
    service_setup: tuple[BridgeService, RuntimeState, AsyncMock, AsyncMock],
) -> None:
    service, state, serial, _ = service_setup
    state.mark_synchronized()

    payload = pb.FileRead(path="test.txt").SerializeToString()

    with patch("pathlib.Path.read_bytes", return_value=b"file_data"):
        with patch("pathlib.Path.is_file", return_value=True):
            await service.handle_mcu_frame(Command.CMD_FILE_READ.value, 1, payload)

    serial.send.assert_called_once()
    assert serial.send.call_args[0][0] == Command.CMD_FILE_READ_RESP.value
    resp = serial.send.call_args[0][1]
    assert isinstance(resp, pb.FileReadResponse)
    assert resp.content == b"file_data"


@pytest.mark.asyncio
async def test_mqtt_file_write_asserts_serial(
    service_setup: tuple[BridgeService, RuntimeState, AsyncMock, AsyncMock],
) -> None:
    service, state, serial, _ = service_setup
    state.mark_synchronized()

    msg = Message(
        topic="br/file/write/mcu/out.txt",
        payload=b"new_data",
        qos=0,
        retain=False,
        mid=1,
        properties=None,
    )

    await service.handle_mqtt_message(msg)

    serial.send.assert_called_once()
    assert serial.send.call_args[0][0] == Command.CMD_FILE_WRITE.value
    req = serial.send.call_args[0][1]
    assert isinstance(req, pb.FileWrite)
    assert req.path == "out.txt"
    assert req.data == b"new_data"


@pytest.mark.asyncio
async def test_mqtt_datastore_put_asserts_cache(
    service_setup: tuple[BridgeService, RuntimeState, AsyncMock, AsyncMock],
) -> None:
    service, state, _, _ = service_setup
    state.mark_synchronized()

    msg = Message(
        topic="br/datastore/put/my_key",
        payload=b"my_value",
        qos=0,
        retain=False,
        mid=1,
        properties=None,
    )

    await service.handle_mqtt_message(msg)

    assert state.datastore_cache is not None
    assert await state.datastore_cache.get("my_key") == b"my_value"


@pytest.mark.asyncio
async def test_mcu_datastore_put_asserts_mqtt(
    service_setup: tuple[BridgeService, RuntimeState, AsyncMock, AsyncMock],
) -> None:
    service, state, _, _ = service_setup
    state.mark_synchronized()
    service.enqueue_mqtt = AsyncMock()

    payload = pb.DatastorePut(key="mcu_key", value=b"mcu_val").SerializeToString()
    await service.handle_mcu_frame(Command.CMD_DATASTORE_PUT.value, 1, payload)

    service.enqueue_mqtt.assert_called_once()
    queued_pub = service.enqueue_mqtt.call_args[0][0]
    assert "br/datastore/get/mcu_key" in queued_pub.topic_name
    assert queued_pub.payload == b"mcu_val"


@pytest.mark.asyncio
async def test_mcu_mailbox_push_asserts_mqtt(
    service_setup: tuple[BridgeService, RuntimeState, AsyncMock, AsyncMock],
) -> None:
    service, state, _, _ = service_setup
    state.mark_synchronized()
    service.enqueue_mqtt = AsyncMock()

    payload = pb.MailboxPush(data=b"mail_data").SerializeToString()
    await service.handle_mcu_frame(Command.CMD_MAILBOX_PUSH.value, 1, payload)

    service.enqueue_mqtt.assert_called_once()
    queued_pub = service.enqueue_mqtt.call_args[0][0]
    assert "br/mailbox/incoming" in queued_pub.topic_name
    assert queued_pub.payload == b"mail_data"


@pytest.mark.asyncio
async def test_mqtt_mailbox_write_asserts_serial(
    service_setup: tuple[BridgeService, RuntimeState, AsyncMock, AsyncMock],
) -> None:
    service, state, serial, _ = service_setup
    state.mark_synchronized()

    msg = Message(
        topic="br/mailbox/write",
        payload=b"incoming_mail",
        qos=0,
        retain=False,
        mid=1,
        properties=None,
    )

    await service.handle_mqtt_message(msg)

    serial.send.assert_called_once()
    assert serial.send.call_args[0][0] == Command.CMD_MAILBOX_PUSH.value
    req = serial.send.call_args[0][1]
    assert isinstance(req, pb.MailboxPush)
    assert req.data == b"incoming_mail"


@pytest.mark.asyncio
async def test_mcu_process_run_asserts_exec(
    service_setup: tuple[BridgeService, RuntimeState, AsyncMock, AsyncMock],
) -> None:
    service, state, serial, _ = service_setup
    state.mark_synchronized()

    payload = pb.ProcessRunAsync(command="echo hello").SerializeToString()

    with patch("mcubridge.services.runtime.is_command_allowed", return_value=True):
        with patch("asyncio.create_subprocess_exec") as mock_exec:
            mock_proc = AsyncMock()
            mock_proc.pid = 1234
            mock_exec.return_value = mock_proc

            await service.handle_mcu_frame(Command.CMD_PROCESS_RUN_ASYNC.value, 1, payload)

            mock_exec.assert_called_once()
            serial.send.assert_called_once()
            assert serial.send.call_args[0][0] == Command.CMD_PROCESS_RUN_ASYNC_RESP.value
            resp = serial.send.call_args[0][1]
            assert isinstance(resp, pb.ProcessRunAsyncResponse)
            assert resp.pid == 1234


@pytest.mark.asyncio
async def test_mqtt_spi_transfer_asserts_serial(
    service_setup: tuple[BridgeService, RuntimeState, AsyncMock, AsyncMock],
) -> None:
    service, state, serial, _ = service_setup
    state.mark_synchronized()

    msg = Message(
        topic="br/spi/transfer",
        payload=b"\x01\x02\x03",
        qos=0,
        retain=False,
        mid=1,
        properties=None,
    )

    await service.handle_mqtt_message(msg)

    serial.send.assert_called_once()
    assert serial.send.call_args[0][0] == Command.CMD_SPI_TRANSFER.value
    req = serial.send.call_args[0][1]
    assert isinstance(req, pb.SpiTransfer)
    assert req.data == b"\x01\x02\x03"


@pytest.mark.asyncio
async def test_mqtt_file_host_write_asserts_cache(
    service_setup: tuple[BridgeService, RuntimeState, AsyncMock, AsyncMock],
) -> None:
    service, state, _, _ = service_setup
    state.mark_synchronized()
    service.enqueue_mqtt = AsyncMock()

    msg = Message(
        topic="br/file/write/host/test.txt",
        payload=b"host_data",
        qos=0,
        retain=False,
        mid=1,
        properties=None,
    )

    with patch("mcubridge.services.runtime.BridgeService._write_with_quota", return_value=True):
        await service.handle_mqtt_message(msg)

    service.enqueue_mqtt.assert_called_once()
    queued_pub = service.enqueue_mqtt.call_args[0][0]
    assert "br/file/read/host/test.txt" in queued_pub.topic_name
    assert queued_pub.payload == b"host_data"


@pytest.mark.asyncio
async def test_mqtt_file_host_read_asserts_read(
    service_setup: tuple[BridgeService, RuntimeState, AsyncMock, AsyncMock],
) -> None:
    service, state, _, _ = service_setup
    state.mark_synchronized()
    service.enqueue_mqtt = AsyncMock()

    msg = Message(
        topic="br/file/read/host/test.txt",
        payload=b"",
        qos=0,
        retain=False,
        mid=1,
        properties=None,
    )

    with patch("pathlib.Path.is_file", return_value=True):
        with patch("pathlib.Path.read_bytes", return_value=b"disk_data"):
            await service.handle_mqtt_message(msg)

    service.enqueue_mqtt.assert_called_once()
    queued_pub = service.enqueue_mqtt.call_args[0][0]
    assert queued_pub.topic_name == "br/file/read/response/host/test.txt"
    assert queued_pub.payload == b"disk_data"


@pytest.mark.asyncio
async def test_mqtt_shell_poll_asserts_mqtt(
    service_setup: tuple[BridgeService, RuntimeState, AsyncMock, AsyncMock],
) -> None:
    service, state, _, _ = service_setup
    state.mark_synchronized()
    service.enqueue_mqtt = AsyncMock()

    msg = Message(
        topic="br/sh/poll/123",
        payload=b"",
        qos=0,
        retain=False,
        mid=1,
        properties=None,
    )

    with patch("mcubridge.services.runtime.is_command_allowed", return_value=True):
        mock_batch = pb.ProcessPollResponse(
            status=0,
            exit_code=0,
            stdout_data=b"out",
            stderr_data=b"err",
            finished=True,
            stdout_truncated=False,
            stderr_truncated=False,
        )
        with patch("mcubridge.services.runtime.BridgeService._poll_process", return_value=mock_batch):
            await service.handle_mqtt_message(msg)

    service.enqueue_mqtt.assert_called_once()
    queued_pub = service.enqueue_mqtt.call_args[0][0]
    assert "br/sh/poll/123/response" in queued_pub.topic_name


@pytest.mark.asyncio
async def test_mqtt_shell_kill_asserts_mqtt(
    service_setup: tuple[BridgeService, RuntimeState, AsyncMock, AsyncMock],
) -> None:
    service, state, _, _ = service_setup
    state.mark_synchronized()

    msg = Message(
        topic="br/sh/kill/123",
        payload=b"",
        qos=0,
        retain=False,
        mid=1,
        properties=None,
    )

    with patch("mcubridge.services.runtime.is_command_allowed", return_value=True):
        with patch("mcubridge.services.runtime.BridgeService._stop_process", return_value=True) as mock_stop:
            await service.handle_mqtt_message(msg)

    mock_stop.assert_called_once_with(123)


@pytest.mark.asyncio
async def test_mqtt_shell_run_asserts_exec(
    service_setup: tuple[BridgeService, RuntimeState, AsyncMock, AsyncMock],
) -> None:
    service, state, _, _ = service_setup
    state.mark_synchronized()
    service.enqueue_mqtt = AsyncMock()

    msg = Message(
        topic="br/sh/run_async",
        payload=b"ls -la",
        qos=0,
        retain=False,
        mid=1,
        properties=None,
    )

    with patch("mcubridge.services.runtime.is_command_allowed", return_value=True):
        with patch("asyncio.create_subprocess_exec") as mock_exec:
            mock_proc = AsyncMock()
            mock_proc.pid = 999
            mock_exec.return_value = mock_proc

            await service.handle_mqtt_message(msg)

            mock_exec.assert_called_once()
            service.enqueue_mqtt.assert_called_once()
            queued_pub = service.enqueue_mqtt.call_args[0][0]
            assert "br/sh/run_async/res" in queued_pub.topic_name
            resp = pb.ProcessRunAsyncResponse.FromString(queued_pub.payload)
            assert resp.pid == 999
