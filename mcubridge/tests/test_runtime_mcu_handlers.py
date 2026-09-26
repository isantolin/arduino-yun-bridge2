"""Tests for runtime MCU frame handling and lifecycle callbacks. [SIL-2]"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from typing import Any
from unittest.mock import AsyncMock

import mcubridge.protocol.mcubridge_pb2 as pb
import pytest
from mcubridge.config.settings import RuntimeConfig
from mcubridge.protocol.protocol import Command, Status
from mcubridge.services.runtime import BridgeService, _PendingMcuRead
from mcubridge.state.context import RuntimeState, create_runtime_state
from mcubridge.transport.serial import SerialTransport
from pytest_mock import MockerFixture


def _make_config() -> RuntimeConfig:
    return RuntimeConfig(
        allowed_commands=("echo", "ls"),
        serial_shared_secret=b"testsharedsecret",
        allow_non_tmp_paths=True,
    )


@pytest.fixture
def mock_config() -> RuntimeConfig:
    return _make_config()


@pytest.fixture
def mock_state(mock_config: RuntimeConfig) -> RuntimeState:
    return create_runtime_state(mock_config)


@pytest.fixture
def svc(mock_config: RuntimeConfig, mock_state: RuntimeState) -> tuple[BridgeService, RuntimeState, AsyncMock]:
    serial = AsyncMock(spec=SerialTransport)
    service = BridgeService(mock_config, mock_state, serial)
    mock_state.connection_fsm.connect()
    mock_state.connection_fsm.synchronize()
    return service, mock_state, serial


@pytest.mark.asyncio
async def test_unsupported_mcu_request_no_serial(svc: tuple[BridgeService, RuntimeState, AsyncMock]) -> None:
    service, _state, _ = svc
    service.serial = None
    unsupported_fn: Callable[[int, int], Awaitable[bool]] = getattr(service, "_unsupported_mcu_request")
    res = await unsupported_fn(1, 0xFF)
    assert res is False


@pytest.mark.asyncio
async def test_unsupported_mcu_request_sends(svc: tuple[BridgeService, RuntimeState, AsyncMock]) -> None:
    service, _state, serial = svc
    serial.send.return_value = True
    unsupported_fn: Callable[[int, int], Awaitable[bool]] = getattr(service, "_unsupported_mcu_request")
    res = await unsupported_fn(1, 0xFF)
    assert res is True
    serial.send.assert_called_once()
    assert serial.send.call_args[0][0] == Status.NOT_IMPLEMENTED.value


@pytest.mark.asyncio
async def test_on_mcu_mailbox_available_no_serial(svc: tuple[BridgeService, RuntimeState, AsyncMock]) -> None:
    service, _state, _ = svc
    service.serial = None
    on_avail_fn: Callable[..., Awaitable[bool]] = getattr(service, "_on_mcu_mailbox_available")
    res = await on_avail_fn(1, None)
    assert res is False


@pytest.mark.asyncio
async def test_on_mcu_mailbox_available_with_items(svc: tuple[BridgeService, RuntimeState, AsyncMock]) -> None:
    service, state, serial = svc
    serial.send.return_value = True
    await state.mailbox_queue.append(b"item1")
    on_avail_fn: Callable[..., Awaitable[bool]] = getattr(service, "_on_mcu_mailbox_available")
    res = await on_avail_fn(1, None)
    assert res is True
    serial.send.assert_called_once()
    assert serial.send.call_args[0][0] == Command.CMD_MAILBOX_AVAILABLE_RESP.value
    resp = serial.send.call_args[0][1]
    assert resp.count == 1


@pytest.mark.asyncio
async def test_on_mcu_mailbox_read_no_serial(svc: tuple[BridgeService, RuntimeState, AsyncMock]) -> None:
    service, _state, _ = svc
    service.serial = None
    on_read_fn: Callable[..., Awaitable[bool]] = getattr(service, "_on_mcu_mailbox_read")
    res = await on_read_fn(1, None)
    assert res is False


@pytest.mark.asyncio
async def test_on_mcu_mailbox_read_empty_queue(svc: tuple[BridgeService, RuntimeState, AsyncMock]) -> None:
    service, _state, serial = svc
    serial.send.return_value = True
    on_read_fn: Callable[..., Awaitable[bool]] = getattr(service, "_on_mcu_mailbox_read")
    res = await on_read_fn(1, None)
    assert res is True
    serial.send.assert_called_once()
    assert serial.send.call_args[0][0] == Command.CMD_MAILBOX_READ_RESP.value
    resp = serial.send.call_args[0][1]
    assert resp.content == b""


@pytest.mark.asyncio
async def test_on_mcu_mailbox_read_with_content(svc: tuple[BridgeService, RuntimeState, AsyncMock]) -> None:
    service, state, serial = svc
    serial.send.return_value = True
    await state.mailbox_queue.append(b"data1")
    await state.mailbox_queue.append(b"data2")
    on_read_fn: Callable[..., Awaitable[bool]] = getattr(service, "_on_mcu_mailbox_read")
    res = await on_read_fn(1, None)
    assert res is True
    serial.send.assert_called_once()
    assert serial.send.call_args[0][0] == Command.CMD_MAILBOX_READ_RESP.value
    resp = serial.send.call_args[0][1]
    assert resp.content == b"data1"


@pytest.mark.asyncio
async def test_on_mcu_mailbox_processed(
    svc: tuple[BridgeService, RuntimeState, AsyncMock], mocker: MockerFixture
) -> None:
    service, state, serial = svc
    serial.send.return_value = True
    p = pb.MailboxProcessed(message_id=1)
    captured: list[pb.CloudQueuedPublish] = []

    async def _cap(msg: pb.CloudQueuedPublish, *args: Any, **kwargs: Any) -> bool:
        captured.append(msg)
        return True

    mocker.patch.object(service, "enqueue_cloud", side_effect=_cap)
    on_proc_fn: Callable[..., Awaitable[bool]] = getattr(service, "_on_mcu_mailbox_processed")
    await on_proc_fn(1, p)
    assert len(captured) == 1
    assert captured[0].topic_name == f"{state.cloud_topic_prefix}/mailbox/processed"


@pytest.mark.asyncio
async def test_on_mcu_file_write_no_serial(svc: tuple[BridgeService, RuntimeState, AsyncMock]) -> None:
    service, _state, _ = svc
    service.serial = None
    p = pb.FileWrite(path="test.txt", data=b"abc")
    on_write_fn: Callable[..., Awaitable[bool]] = getattr(service, "_on_mcu_file_write")
    res = await on_write_fn(1, p)
    assert res is False


@pytest.mark.asyncio
async def test_on_mcu_file_write_unsafe_path(svc: tuple[BridgeService, RuntimeState, AsyncMock]) -> None:
    service, state, serial = svc
    state.allow_non_tmp_paths = False
    service.config.file_system_root = "/tmp/fs"
    serial.send.return_value = True
    p = pb.FileWrite(path="../../../etc/passwd", data=b"evil")
    on_write_fn: Callable[..., Awaitable[bool]] = getattr(service, "_on_mcu_file_write")
    res = await on_write_fn(1, p)
    assert res is True
    serial.send.assert_called_once()
    assert serial.send.call_args[0][0] == Status.ERROR.value


@pytest.mark.asyncio
async def test_on_mcu_file_write_success(svc: tuple[BridgeService, RuntimeState, AsyncMock]) -> None:
    service, _state, serial = svc
    serial.send.return_value = True
    p = pb.FileWrite(path="output.txt", data=b"valid data")
    on_write_fn: Callable[..., Awaitable[bool]] = getattr(service, "_on_mcu_file_write")
    res = await on_write_fn(1, p)
    assert res is True
    serial.send.assert_called_once()
    assert serial.send.call_args[0][0] == Status.OK.value


@pytest.mark.asyncio
async def test_on_mcu_file_read_no_serial(svc: tuple[BridgeService, RuntimeState, AsyncMock]) -> None:
    service, _state, serial = svc
    service.serial = None
    p = pb.FileRead(path="missing.txt")
    on_read_fn: Callable[..., Awaitable[None]] = getattr(service, "_on_mcu_file_read")
    await on_read_fn(1, p)
    assert service.serial is None
    serial.send.assert_not_called()


@pytest.mark.asyncio
async def test_on_mcu_file_read_missing_file(svc: tuple[BridgeService, RuntimeState, AsyncMock]) -> None:
    service, _state, serial = svc
    serial.send.return_value = True
    p = pb.FileRead(path="nonexistent.txt")
    on_read_fn: Callable[..., Awaitable[None]] = getattr(service, "_on_mcu_file_read")
    await on_read_fn(1, p)
    serial.send.assert_called_once()
    assert serial.send.call_args[0][0] == Status.ERROR.value


@pytest.mark.asyncio
async def test_on_mcu_file_read_existing_nonempty(svc: tuple[BridgeService, RuntimeState, AsyncMock]) -> None:
    service, _state, serial = svc
    serial.send.return_value = True
    await service.safe_file_write("exists.txt", b"chunk-payload")
    p = pb.FileRead(path="exists.txt")
    on_read_fn: Callable[..., Awaitable[None]] = getattr(service, "_on_mcu_file_read")
    await on_read_fn(1, p)
    assert serial.send.call_count >= 1
    call1 = serial.send.call_args_list[0]
    assert call1[0][0] == Command.CMD_FILE_READ_RESP.value
    resp = call1[0][1]
    assert resp.content == b"chunk-payload"


@pytest.mark.asyncio
async def test_on_mcu_file_read_existing_empty(svc: tuple[BridgeService, RuntimeState, AsyncMock]) -> None:
    service, _state, serial = svc
    serial.send.return_value = True
    await service.safe_file_write("empty.txt", b"")
    p = pb.FileRead(path="empty.txt")
    on_read_fn: Callable[..., Awaitable[None]] = getattr(service, "_on_mcu_file_read")
    await on_read_fn(1, p)
    serial.send.assert_called_once()
    assert serial.send.call_args[0][0] == Command.CMD_FILE_READ_RESP.value
    resp = serial.send.call_args[0][1]
    assert resp.content == b""


@pytest.mark.asyncio
async def test_on_mcu_file_remove_no_serial(svc: tuple[BridgeService, RuntimeState, AsyncMock]) -> None:
    service, _state, _ = svc
    service.serial = None
    p = pb.FileRemove(path="missing.txt")
    on_remove_fn: Callable[..., Awaitable[bool]] = getattr(service, "_on_mcu_file_remove")
    res = await on_remove_fn(1, p)
    assert res is False


@pytest.mark.asyncio
async def test_on_mcu_file_remove_missing(svc: tuple[BridgeService, RuntimeState, AsyncMock]) -> None:
    service, _state, serial = svc
    serial.send.return_value = True
    p = pb.FileRemove(path="nonexistent.txt")
    on_remove_fn: Callable[..., Awaitable[bool]] = getattr(service, "_on_mcu_file_remove")
    res = await on_remove_fn(1, p)
    assert res is True
    serial.send.assert_called_once()
    assert serial.send.call_args[0][0] == Status.ERROR.value


@pytest.mark.asyncio
async def test_on_mcu_file_remove_existing(svc: tuple[BridgeService, RuntimeState, AsyncMock]) -> None:
    service, _state, serial = svc
    serial.send.return_value = True
    await service.safe_file_write("to_del.txt", b"bye")
    p = pb.FileRemove(path="to_del.txt")
    on_remove_fn: Callable[..., Awaitable[bool]] = getattr(service, "_on_mcu_file_remove")
    res = await on_remove_fn(1, p)
    assert res is True
    serial.send.assert_called_once()
    assert serial.send.call_args[0][0] == Status.OK.value


@pytest.mark.asyncio
async def test_on_mcu_file_read_resp_no_pending(svc: tuple[BridgeService, RuntimeState, AsyncMock]) -> None:
    service, _state, _ = svc
    p = pb.FileReadResponse(content=b"orphan")
    on_read_resp: Callable[..., Awaitable[bool]] = getattr(service, "_on_mcu_file_read_resp")
    res = await on_read_resp(1, p)
    assert res is False


@pytest.mark.asyncio
async def test_on_mcu_file_read_resp_accumulates_chunks(svc: tuple[BridgeService, RuntimeState, AsyncMock]) -> None:
    service, _state, _ = svc
    fut: asyncio.Future[bytes] = asyncio.Future()
    pending = _PendingMcuRead(future=fut, chunks=[])
    setattr(service, "_pending_mcu_read", pending)
    p = pb.FileReadResponse(content=b"chunk1")
    on_read_resp: Callable[..., Awaitable[bool]] = getattr(service, "_on_mcu_file_read_resp")
    res = await on_read_resp(1, p)
    assert res is True
    assert not fut.done()
    assert pending.chunks == [b"chunk1"]


@pytest.mark.asyncio
async def test_on_mcu_file_read_resp_completes_future(svc: tuple[BridgeService, RuntimeState, AsyncMock]) -> None:
    service, _state, _ = svc
    fut: asyncio.Future[bytes] = asyncio.Future()
    pending = _PendingMcuRead(future=fut, chunks=[b"chunk1"])
    setattr(service, "_pending_mcu_read", pending)
    p = pb.FileReadResponse(content=b"")  # EOF
    on_read_resp: Callable[..., Awaitable[bool]] = getattr(service, "_on_mcu_file_read_resp")
    res = await on_read_resp(1, p)
    assert res is True
    assert fut.done()
    assert fut.result() == b"chunk1"


@pytest.mark.asyncio
async def test_on_mcu_ack_valid(svc: tuple[BridgeService, RuntimeState, AsyncMock], mocker: MockerFixture) -> None:
    service, _state, _serial = svc
    p = pb.AckPacket(command_id=0x01)
    mock_debug = mocker.patch("mcubridge.services.runtime.logger.debug")
    on_ack: Callable[..., Awaitable[None]] = getattr(service, "_on_mcu_ack")
    await on_ack(1, p)
    assert mock_debug.called


@pytest.mark.asyncio
async def test_on_mcu_ack_raw_bytes(svc: tuple[BridgeService, RuntimeState, AsyncMock], mocker: MockerFixture) -> None:
    service, _state, _serial = svc
    valid_bytes = pb.AckPacket(command_id=0x02).SerializeToString()
    mock_debug = mocker.patch("mcubridge.services.runtime.logger.debug")
    on_ack: Callable[..., Awaitable[None]] = getattr(service, "_on_mcu_ack")
    await on_ack(1, valid_bytes)
    assert mock_debug.called


@pytest.mark.asyncio
async def test_on_mcu_ack_corrupt_bytes(
    svc: tuple[BridgeService, RuntimeState, AsyncMock], mocker: MockerFixture
) -> None:
    service, _state, _serial = svc
    mock_err = mocker.patch("mcubridge.services.runtime.logger.error")
    on_ack: Callable[..., Awaitable[None]] = getattr(service, "_on_mcu_ack")
    await on_ack(1, b"\xff\xff\xff")
    assert mock_err.called


@pytest.mark.asyncio
async def test_handle_mcu_status_ok_no_payload(
    svc: tuple[BridgeService, RuntimeState, AsyncMock], mocker: MockerFixture
) -> None:
    service, state, _serial = svc
    captured: list[pb.CloudQueuedPublish] = []

    async def _cap(msg: pb.CloudQueuedPublish, *args: Any, **kwargs: Any) -> bool:
        captured.append(msg)
        return True

    mocker.patch.object(service, "enqueue_cloud", side_effect=_cap)
    handle_status: Callable[..., Awaitable[bool]] = getattr(service, "_handle_mcu_status")
    await handle_status(Status.OK, 1, b"")
    assert len(captured) == 1
    assert captured[0].topic_name == f"{state.cloud_topic_prefix}/system/status"
    report = pb.StatusReport.FromString(captured[0].payload)
    assert report.status == Status.OK.value


@pytest.mark.asyncio
async def test_handle_mcu_status_error_with_generic_response(
    svc: tuple[BridgeService, RuntimeState, AsyncMock], mocker: MockerFixture
) -> None:
    service, state, _serial = svc
    captured: list[pb.CloudQueuedPublish] = []

    async def _cap(msg: pb.CloudQueuedPublish, *args: Any, **kwargs: Any) -> bool:
        captured.append(msg)
        return True

    mocker.patch.object(service, "enqueue_cloud", side_effect=_cap)
    p = pb.GenericResponse(status="error", message="hardware fault")
    handle_status: Callable[..., Awaitable[bool]] = getattr(service, "_handle_mcu_status")
    await handle_status(Status.ERROR, 1, p)
    assert len(captured) == 1
    assert captured[0].topic_name == f"{state.cloud_topic_prefix}/system/status"
    report = pb.StatusReport.FromString(captured[0].payload)
    assert report.message == "hardware fault"


@pytest.mark.asyncio
async def test_handle_mcu_status_error_with_protobuf_message(
    svc: tuple[BridgeService, RuntimeState, AsyncMock], mocker: MockerFixture
) -> None:
    service, state, _serial = svc
    captured: list[pb.CloudQueuedPublish] = []

    async def _cap(msg: pb.CloudQueuedPublish, *args: Any, **kwargs: Any) -> bool:
        captured.append(msg)
        return True

    mocker.patch.object(service, "enqueue_cloud", side_effect=_cap)
    p = pb.AckPacket(command_id=0x05)
    handle_status: Callable[..., Awaitable[bool]] = getattr(service, "_handle_mcu_status")
    await handle_status(Status.OK, 1, p)
    assert len(captured) == 1
    assert captured[0].topic_name == f"{state.cloud_topic_prefix}/system/status"
    report = pb.StatusReport.FromString(captured[0].payload)
    assert report.status == Status.OK.value


@pytest.mark.asyncio
async def test_handle_mcu_status_with_hex_payload(
    svc: tuple[BridgeService, RuntimeState, AsyncMock], mocker: MockerFixture
) -> None:
    service, state, _serial = svc
    captured: list[pb.CloudQueuedPublish] = []

    async def _cap(msg: pb.CloudQueuedPublish, *args: Any, **kwargs: Any) -> bool:
        captured.append(msg)
        return True

    mocker.patch.object(service, "enqueue_cloud", side_effect=_cap)
    raw = b"\xca\xfe\xba\xbe"
    handle_status: Callable[..., Awaitable[bool]] = getattr(service, "_handle_mcu_status")
    await handle_status(Status.OK, 1, raw)
    assert len(captured) == 1
    assert captured[0].topic_name == f"{state.cloud_topic_prefix}/system/status"
    report = pb.StatusReport.FromString(captured[0].payload)
    assert report.message == f"<hex:{raw.hex()}>"


@pytest.mark.asyncio
async def test_on_mcu_digital_read_resp(
    svc: tuple[BridgeService, RuntimeState, AsyncMock], mocker: MockerFixture
) -> None:
    service, state, _serial = svc
    captured: list[pb.CloudQueuedPublish] = []

    async def _cap(msg: pb.CloudQueuedPublish, *args: Any, **kwargs: Any) -> bool:
        captured.append(msg)
        return True

    mocker.patch.object(service, "enqueue_cloud", side_effect=_cap)
    from mcubridge.protocol.structures import PendingPinRequest

    req = PendingPinRequest(pin=13, reply_context=None)
    state.pending_digital_reads.append(req)

    p = pb.DigitalReadResponse(value=1)
    on_digital_read: Callable[..., Awaitable[None]] = getattr(service, "_on_mcu_digital_read_resp")
    await on_digital_read(1, p)
    assert len(captured) == 1
    assert captured[0].topic_name == f"{state.cloud_topic_prefix}/d/13/value"
    assert captured[0].payload == b"1"


@pytest.mark.asyncio
async def test_on_mcu_analog_read_resp(
    svc: tuple[BridgeService, RuntimeState, AsyncMock], mocker: MockerFixture
) -> None:
    service, state, _serial = svc
    captured: list[pb.CloudQueuedPublish] = []

    async def _cap(msg: pb.CloudQueuedPublish, *args: Any, **kwargs: Any) -> bool:
        captured.append(msg)
        return True

    mocker.patch.object(service, "enqueue_cloud", side_effect=_cap)
    from mcubridge.protocol.structures import PendingPinRequest

    req = PendingPinRequest(pin=2, reply_context=None)
    state.pending_analog_reads.append(req)

    p = pb.AnalogReadResponse(value=1023)
    on_analog_read: Callable[..., Awaitable[None]] = getattr(service, "_on_mcu_analog_read_resp")
    await on_analog_read(1, p)
    assert len(captured) == 1
    assert captured[0].topic_name == f"{state.cloud_topic_prefix}/a/2/value"
    assert captured[0].payload == b"1023"


@pytest.mark.asyncio
async def test_on_mcu_spi_resp(svc: tuple[BridgeService, RuntimeState, AsyncMock], mocker: MockerFixture) -> None:
    service, state, _serial = svc
    captured: list[pb.CloudQueuedPublish] = []

    async def _cap(msg: pb.CloudQueuedPublish, *args: Any, **kwargs: Any) -> bool:
        captured.append(msg)
        return True

    mocker.patch.object(service, "enqueue_cloud", side_effect=_cap)
    p = pb.SpiTransferResponse(data=b"\xde\xad")
    on_spi_resp: Callable[..., Awaitable[None]] = getattr(service, "_on_mcu_spi_transfer_resp")
    await on_spi_resp(1, p)
    assert len(captured) == 1
    assert captured[0].topic_name == f"{state.cloud_topic_prefix}/spi/transfer/resp"
    assert captured[0].payload == b"\xde\xad"


@pytest.mark.asyncio
async def test_on_mcu_process_run_async_no_serial(svc: tuple[BridgeService, RuntimeState, AsyncMock]) -> None:
    service, _state, _ = svc
    service.serial = None
    p = pb.ProcessRunAsync(command="echo hi")
    on_run_async: Callable[..., Awaitable[bool]] = getattr(service, "_on_mcu_process_run_async")
    res = await on_run_async(1, p)
    assert res is False


@pytest.mark.asyncio
async def test_on_mcu_process_run_async_disallowed(svc: tuple[BridgeService, RuntimeState, AsyncMock]) -> None:
    service, _state, serial = svc
    serial.send.return_value = True
    p = pb.ProcessRunAsync(command="rm -rf /")
    on_run_async: Callable[..., Awaitable[bool]] = getattr(service, "_on_mcu_process_run_async")
    res = await on_run_async(1, p)
    assert res is False
    serial.send.assert_called_once()
    assert serial.send.call_args[0][0] == Status.ERROR.value


@pytest.mark.asyncio
async def test_on_mcu_process_run_async_allowed(
    svc: tuple[BridgeService, RuntimeState, AsyncMock], mocker: MockerFixture
) -> None:
    service, _state, serial = svc
    serial.send.return_value = True
    p = pb.ProcessRunAsync(command="echo hello")
    mocker.patch.object(service, "run_process", new=AsyncMock(return_value=1234))
    on_run_async: Callable[..., Awaitable[bool]] = getattr(service, "_on_mcu_process_run_async")
    result = await on_run_async(1, p)
    assert result is True
    args = serial.send.call_args[0]
    assert args[0] == Command.CMD_PROCESS_RUN_ASYNC_RESP.value
    assert isinstance(args[1], pb.ProcessRunAsyncResponse)
    assert args[1].pid == 1234


@pytest.mark.asyncio
async def test_on_mcu_process_run_async_pid_zero(
    svc: tuple[BridgeService, RuntimeState, AsyncMock], mocker: MockerFixture
) -> None:
    service, _state, serial = svc
    serial.send.return_value = True
    p = pb.ProcessRunAsync(command="echo hello")
    mocker.patch.object(service, "run_process", new=AsyncMock(return_value=0))
    on_run_async: Callable[..., Awaitable[bool]] = getattr(service, "_on_mcu_process_run_async")
    result = await on_run_async(1, p)
    assert result is False


@pytest.mark.asyncio
async def test_on_mcu_process_poll_no_serial(svc: tuple[BridgeService, RuntimeState, AsyncMock]) -> None:
    service, _state, _ = svc
    service.serial = None
    p = pb.ProcessPoll(pid=10)
    on_poll: Callable[..., Awaitable[bool]] = getattr(service, "_on_mcu_process_poll")
    res = await on_poll(1, p)
    assert res is False


@pytest.mark.asyncio
async def test_on_mcu_process_poll_with_result(
    svc: tuple[BridgeService, RuntimeState, AsyncMock], mocker: MockerFixture
) -> None:
    service, _state, serial = svc
    serial.send.return_value = True
    mock_batch = pb.ProcessPollResponse(finished=True, exit_code=0)
    p = pb.ProcessPoll(pid=42)
    mocker.patch.object(service, "poll_process", new=AsyncMock(return_value=mock_batch))
    on_poll: Callable[..., Awaitable[bool]] = getattr(service, "_on_mcu_process_poll")
    result = await on_poll(1, p)
    assert result is True
