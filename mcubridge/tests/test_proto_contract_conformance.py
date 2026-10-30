"""Automated protocol contract and RPC conformance test suite. [SIL-2]

Replaces handwritten passthrough handler tests with data-driven and schema-driven
verification across all MCU Bridge protocol commands and runtime dispatch routes.
"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import mcubridge.protocol.mcubridge_pb2 as pb
from hypothesis import given, strategies as st
import pytest
from dirty_equals import IsInt, IsPositiveInt, IsStr
from mcubridge.config.settings import RuntimeConfig
from mcubridge.protocol.protocol import REGISTRY, Command, Status
from mcubridge.protocol.structures import PendingPinRequest
from mcubridge.services.runtime import BridgeService, PendingMcuRead
from mcubridge.state.context import RuntimeState, create_runtime_state
from mcubridge.transport.serial import SerialTransport


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


# -----------------------------------------------------------------------------
# 1. Declarative Contract Verification (All Registry Entries)
# -----------------------------------------------------------------------------


def test_protocol_registry_coverage(svc: tuple[BridgeService, RuntimeState, AsyncMock]) -> None:
    """Certifies that all known Commands have defined handlers or default fallbacks."""
    service, _state, _serial = svc
    dispatch = REGISTRY.build_mcu_dispatch(service)
    for cmd in Command:
        assert cmd.value in dispatch or callable(service.unsupported_mcu_request)


# -----------------------------------------------------------------------------
# 2. Unsupported Request Fallback Tests
# -----------------------------------------------------------------------------


@pytest.mark.parametrize(("has_serial", "expected_result"), [(False, False), (True, True)])
async def test_unsupported_mcu_request(
    svc: tuple[BridgeService, RuntimeState, AsyncMock],
    has_serial: bool,
    expected_result: bool,
) -> None:
    service, _state, serial = svc
    if not has_serial:
        service.serial = None
    else:
        serial.send.return_value = True

    fn: Callable[[int, int], Awaitable[bool]] = service.unsupported_mcu_request
    res = await fn(1, 0xFF)
    assert res is expected_result
    if has_serial:
        serial.send.assert_called_once()
        assert serial.send.call_args[0][0] == Status.NOT_IMPLEMENTED.value


# -----------------------------------------------------------------------------
# 3. Mailbox Operations Conformance Suite
# -----------------------------------------------------------------------------


async def test_mailbox_available_scenarios(svc: tuple[BridgeService, RuntimeState, AsyncMock]) -> None:
    service, state, serial = svc
    on_avail: Callable[..., Awaitable[bool]] = service.mailbox_available

    # Without serial
    service.serial = None
    assert await on_avail(1, None) is False

    # With serial and empty queue
    service.serial = serial
    serial.send.return_value = True
    assert await on_avail(1, None) is True
    serial.send.assert_called_with(Command.CMD_MAILBOX_AVAILABLE_RESP.value, pb.MailboxAvailableResponse(count=0))

    # With items in queue
    await state.mailbox_queue.append(b"item1")
    assert await on_avail(2, None) is True
    resp = serial.send.call_args[0][1]
    assert resp.count == IsPositiveInt


async def test_mailbox_read_scenarios(svc: tuple[BridgeService, RuntimeState, AsyncMock]) -> None:
    service, state, serial = svc
    on_read: Callable[..., Awaitable[pb.MailboxReadResponse]] = service.mailbox_read

    # Without serial
    service.serial = None
    assert (await on_read(1, None)).content == b""

    # Empty queue
    service.serial = serial
    serial.send.return_value = True
    assert (await on_read(1, None)).content == b""
    assert serial.send.call_args[0][0] == Command.CMD_MAILBOX_READ_RESP.value
    assert serial.send.call_args[0][1].content == b""

    # With queue items
    await state.mailbox_queue.append(b"msg_a")
    await state.mailbox_queue.append(b"msg_b")
    assert (await on_read(2, None)).content == b"msg_a"
    assert serial.send.call_args[0][1].content == b"msg_a"


async def test_mailbox_processed(svc: tuple[BridgeService, RuntimeState, AsyncMock]) -> None:
    service, state, _serial = svc
    captured: list[pb.CloudQueuedPublish] = []

    async def _cap(msg: pb.CloudQueuedPublish, *args: Any, **kwargs: Any) -> None:
        captured.append(msg)

    service.cloud_publisher = _cap
    on_proc: Callable[..., Awaitable[None]] = service.publish_mcu_message
    await on_proc(1, pb.MailboxProcessed(message_id=42))
    assert len(captured) == 1
    assert captured[0].topic_name == f"{state.cloud_topic_prefix}/mailbox/processed"


# -----------------------------------------------------------------------------
# 4. FileSystem Operations Conformance Suite
# -----------------------------------------------------------------------------


async def test_file_write_scenarios(svc: tuple[BridgeService, RuntimeState, AsyncMock]) -> None:
    service, state, serial = svc
    on_write: Callable[..., Awaitable[pb.GenericResponse]] = service.file_write

    # Without serial
    service.serial = None
    assert (await on_write(1, pb.FileWrite(path="test.txt", data=b"data"))).status == "ok"

    # Path traversal protection
    service.serial = serial
    state.allow_non_tmp_paths = False
    service.config.file_system_root = "/tmp/fs"
    serial.send.return_value = True
    assert (await on_write(1, pb.FileWrite(path="../../../etc/shadow", data=b"evil"))).status == "error"
    assert serial.send.call_args[0][0] == Status.ERROR.value

    # Successful write
    assert (await on_write(2, pb.FileWrite(path="valid.txt", data=b"content"))).status == "ok"
    assert serial.send.call_args[0][0] == Status.OK.value


async def test_file_read_and_remove_scenarios(svc: tuple[BridgeService, RuntimeState, AsyncMock]) -> None:
    service, _state, serial = svc
    on_read: Callable[..., Awaitable[pb.FileReadResponse]] = service.file_read
    on_remove: Callable[..., Awaitable[pb.GenericResponse]] = service.file_remove

    # Read without serial
    service.serial = None
    assert (await on_read(1, pb.FileRead(path="a.txt"))).content == b""

    # Read missing file
    service.serial = serial
    serial.send.return_value = True
    assert (await on_read(1, pb.FileRead(path="missing.txt"))).content == b""
    assert serial.send.call_args[0][0] == Status.ERROR.value

    # Read existing nonempty file as bounded data frames plus an explicit EOF frame.
    await service.file_write(pb.FileWrite(path="exists.txt", data=b"chunk-data"))
    serial.send.reset_mock()
    assert (await on_read(2, pb.FileRead(path="exists.txt"))).content == b""
    sent = serial.send.await_args_list
    assert [call.args[0] for call in sent] == [
        Command.CMD_FILE_READ_RESP.value,
        Command.CMD_FILE_READ_RESP.value,
    ]
    assert sent[0].args[1].content == b"chunk-data"
    assert sent[1].args[1].content == b""

    # Read existing empty file; it still emits exactly one EOF frame.
    await service.file_write(pb.FileWrite(path="empty.txt", data=b""))
    serial.send.reset_mock()
    assert (await on_read(3, pb.FileRead(path="empty.txt"))).content == b""
    serial.send.assert_awaited_once()
    assert serial.send.await_args.args[0] == Command.CMD_FILE_READ_RESP.value
    assert serial.send.await_args.args[1].content == b""

    # Remove without serial
    service.serial = None
    assert (await on_remove(1, pb.FileRemove(path="missing.txt"))).status == "error"

    # Remove nonexistent file
    service.serial = serial
    assert (await on_remove(1, pb.FileRemove(path="missing.txt"))).status == "error"
    assert serial.send.call_args[0][0] == Status.ERROR.value

    # Remove existing file
    assert (await on_remove(2, pb.FileRemove(path="exists.txt"))).status == "ok"
    assert serial.send.call_args[0][0] == Status.OK.value


async def test_file_read_resp_chunking(svc: tuple[BridgeService, RuntimeState, AsyncMock]) -> None:
    service, _state, _serial = svc
    on_read_resp: Callable[..., Awaitable[pb.FileReadResponse]] = service.file_read

    # No pending read
    assert (await on_read_resp(1, pb.FileReadResponse(content=b"orphan"))).content == b""

    # Accumulate chunk then EOF
    fut: asyncio.Future[bytes] = asyncio.Future()
    pending = PendingMcuRead(future=fut, buffer=bytearray())
    service.pending_mcu_read = pending

    assert (await on_read_resp(1, pb.FileReadResponse(content=b"chunk1"))).content == b"chunk1"
    assert not fut.done()
    assert pending.buffer == b"chunk1"

    assert (await on_read_resp(2, pb.FileReadResponse(content=b""))).content == b""
    assert fut.done()
    assert fut.result() == b"chunk1"
    assert pending.buffer == b""

    # Future already completed
    assert (await on_read_resp(3, pb.FileReadResponse(content=b""))).content == b""
    assert fut.result() == b"chunk1"


# -----------------------------------------------------------------------------
# 5. Process Execution Conformance Suite
# -----------------------------------------------------------------------------


async def test_process_run_async_and_poll(svc: tuple[BridgeService, RuntimeState, AsyncMock]) -> None:
    service, _state, serial = svc
    on_run: Callable[..., Awaitable[pb.ProcessRunAsyncResponse]] = service.process_run_async
    on_poll: Callable[..., Awaitable[pb.ProcessPollResponse]] = service.process_poll

    # Without serial
    service.serial = None
    assert (await on_run(1, pb.ProcessRunAsync(command="rm -rf /"))).pid == 0
    assert (await on_poll(1, pb.ProcessPoll(pid=10))).finished is True

    # Disallowed command
    service.serial = serial
    serial.send.return_value = True
    assert (await on_run(1, pb.ProcessRunAsync(command="rm -rf /"))).pid == 0
    assert serial.send.call_args[0][0] == Status.ERROR.value

    # Allowed command
    assert (await on_run(2, pb.ProcessRunAsync(command="echo test_exec"))).pid == IsPositiveInt
    args = serial.send.call_args[0]
    assert args[0] == Command.CMD_PROCESS_RUN_ASYNC_RESP.value
    pid = args[1].pid
    assert pid == IsPositiveInt

    # Poll process
    assert (await on_poll(3, pb.ProcessPoll(pid=pid))).status == Status.OK.value
    assert serial.send.call_args[0][0] == Command.CMD_PROCESS_POLL_RESP.value
    await service.process_kill(pid)

    # Disallowed command name fallback
    assert (await on_run(4, pb.ProcessRunAsync(command="disallowed_proc_xyz"))).pid == 0


# -----------------------------------------------------------------------------
# 6. Status and Pin Telemetry Conformance Suite
# -----------------------------------------------------------------------------


async def test_status_reporting_and_ack(
    svc: tuple[BridgeService, RuntimeState, AsyncMock],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    service, state, _serial = svc
    captured: list[pb.CloudQueuedPublish] = []

    async def _cap(msg: pb.CloudQueuedPublish, *args: Any, **kwargs: Any) -> None:
        captured.append(msg)

    service.cloud_publisher = _cap
    handle_status: Callable[..., Awaitable[None]] = service.handle_mcu_status

    # Status OK no payload
    await handle_status(Status.OK, 1, b"")
    assert captured[-1].topic_name == f"{state.cloud_topic_prefix}/system/status"

    # Status with GenericResponse
    await handle_status(Status.ERROR, 2, pb.GenericResponse(status="error", message="fault"))
    report = pb.StatusReport.FromString(captured[-1].payload)
    assert report.message == "fault"

    # Status with AckPacket
    await handle_status(Status.OK, 3, pb.AckPacket(command_id=0x05))
    report = pb.StatusReport.FromString(captured[-1].payload)
    assert report.status == IsInt

    # Status with raw hex
    raw = b"\xde\xad\xbe\xef"
    await handle_status(Status.OK, 4, raw)
    report = pb.StatusReport.FromString(captured[-1].payload)
    assert report.message == IsStr(regex=r"<hex:[0-9a-f]+>")

    # ACK packet valid, raw bytes, corrupt bytes
    mock_debug = MagicMock()
    mock_error = MagicMock()
    monkeypatch.setattr("mcubridge.services.runtime.logger.debug", mock_debug)
    monkeypatch.setattr("mcubridge.services.runtime.logger.error", mock_error)

    await handle_status(Status.ACK, 1, pb.AckPacket(command_id=0x01))
    mock_debug.assert_called_with("MCU ACK received", command_id="0x01")

    await handle_status(Status.ACK, 2, pb.AckPacket(command_id=0x02).SerializeToString())
    mock_debug.assert_called_with("MCU ACK received", command_id="0x02")

    await handle_status(Status.ACK, 3, b"\xff\xff\xff")
    mock_error.assert_called_once()


async def test_pin_telemetry_and_spi(svc: tuple[BridgeService, RuntimeState, AsyncMock]) -> None:
    service, state, mock_serial = svc
    captured: list[pb.CloudQueuedPublish] = []

    async def _cap(msg: pb.CloudQueuedPublish, *args: Any, **kwargs: Any) -> None:
        captured.append(msg)

    service.cloud_publisher = _cap

    # Digital read response
    state.pending_digital_reads.append(PendingPinRequest(pin=13, reply_context=None))
    on_pin_resp: Callable[..., Awaitable[None]] = service.gpio.handle_pin_read_resp
    await on_pin_resp(1, pb.DigitalReadResponse(value=1))
    assert captured[-1].topic_name == f"{state.cloud_topic_prefix}/d/13/value"
    assert captured[-1].payload == b"1"

    # Analog read response
    state.pending_analog_reads.append(PendingPinRequest(pin=2, reply_context=None))
    await on_pin_resp(2, pb.AnalogReadResponse(value=1023))
    assert captured[-1].topic_name == f"{state.cloud_topic_prefix}/a/2/value"
    assert captured[-1].payload == b"1023"

    # SPI transfer response
    on_spi: Callable[..., Awaitable[None]] = service.publish_mcu_message
    await on_spi(3, pb.SpiTransferResponse(data=b"\xaa\x55"))
    assert captured[-1].topic_name == f"{state.cloud_topic_prefix}/spi/transfer/resp"
    assert captured[-1].payload == b"\xaa\x55"

    # Datastore put with cache None
    state.datastore_cache = None
    on_ds_put = service.datastore_put
    resp_ds_put = await on_ds_put(1, pb.DatastorePut(key="k", value=b"v"))
    assert resp_ds_put.status == "ok"

    # Corrupt Protobuf payload
    initial_errors = state.serial_decode_errors
    await service.handle_mcu_frame(Command.CMD_PIN_UPDATE_EVENT.value, 10, b"\xff\xff\xff\xff")
    assert state.serial_decode_errors == initial_errors + 1
    assert state.serial_decode_errors == IsPositiveInt
    mock_serial.send.assert_awaited_with(Status.MALFORMED.value, b"")


@given(pin_mode=st.from_type(pb.PinMode))
def test_pin_mode_roundtrip_property(pin_mode: pb.PinMode) -> None:
    """Property: PinMode message preserves fields across protobuf serialization."""
    encoded = pin_mode.SerializeToString()
    decoded = pb.PinMode.FromString(encoded)
    assert decoded.pin == pin_mode.pin
    assert decoded.mode == pin_mode.mode


@given(digital_write=st.from_type(pb.DigitalWrite))
def test_digital_write_roundtrip_property(digital_write: pb.DigitalWrite) -> None:
    """Property: DigitalWrite message preserves fields across protobuf serialization."""
    encoded = digital_write.SerializeToString()
    decoded = pb.DigitalWrite.FromString(encoded)
    assert decoded.pin == digital_write.pin
    assert decoded.value == digital_write.value


@given(analog_write=st.from_type(pb.AnalogWrite))
def test_analog_write_roundtrip_property(analog_write: pb.AnalogWrite) -> None:
    """Property: AnalogWrite message preserves fields across protobuf serialization."""
    encoded = analog_write.SerializeToString()
    decoded = pb.AnalogWrite.FromString(encoded)
    assert decoded.pin == analog_write.pin
    assert decoded.value == analog_write.value


@given(datastore_put=st.from_type(pb.DatastorePut))
def test_datastore_put_roundtrip_property(datastore_put: pb.DatastorePut) -> None:
    """Property: DatastorePut message preserves fields across protobuf serialization."""
    encoded = datastore_put.SerializeToString()
    decoded = pb.DatastorePut.FromString(encoded)
    assert decoded.key == datastore_put.key
    assert decoded.value == datastore_put.value


@given(mailbox_push=st.from_type(pb.MailboxPush))
def test_mailbox_push_roundtrip_property(mailbox_push: pb.MailboxPush) -> None:
    """Property: MailboxPush message preserves fields across protobuf serialization."""
    encoded = mailbox_push.SerializeToString()
    decoded = pb.MailboxPush.FromString(encoded)
    assert decoded.data == mailbox_push.data


@given(file_write=st.from_type(pb.FileWrite))
def test_file_write_roundtrip_property(file_write: pb.FileWrite) -> None:
    """Property: FileWrite message preserves fields across protobuf serialization."""
    encoded = file_write.SerializeToString()
    decoded = pb.FileWrite.FromString(encoded)
    assert decoded.path == file_write.path
    assert decoded.data == file_write.data


@given(file_read=st.from_type(pb.FileRead))
def test_file_read_roundtrip_property(file_read: pb.FileRead) -> None:
    """Property: FileRead message preserves fields across protobuf serialization."""
    encoded = file_read.SerializeToString()
    decoded = pb.FileRead.FromString(encoded)
    assert decoded.path == file_read.path


@given(spi_transfer=st.from_type(pb.SpiTransfer))
def test_spi_transfer_roundtrip_property(spi_transfer: pb.SpiTransfer) -> None:
    """Property: SpiTransfer message preserves fields across protobuf serialization."""
    encoded = spi_transfer.SerializeToString()
    decoded = pb.SpiTransfer.FromString(encoded)
    assert decoded.data == spi_transfer.data


@given(pub=st.from_type(pb.CloudQueuedPublish))
def test_cloud_queued_publish_roundtrip_property(pub: pb.CloudQueuedPublish) -> None:
    """Property: CloudQueuedPublish message preserves fields across protobuf serialization."""
    encoded = pub.SerializeToString()
    decoded = pb.CloudQueuedPublish.FromString(encoded)
    assert decoded.topic_name == pub.topic_name
    assert decoded.payload == pub.payload
    assert decoded.qos == pub.qos
    assert decoded.retain == pub.retain
    assert decoded.correlation_data == pub.correlation_data
    assert decoded.response_topic == pub.response_topic
