"""Automated protocol contract and RPC conformance test suite. [SIL-2]

Replaces handwritten passthrough handler tests with data-driven and schema-driven
verification across all MCU Bridge protocol commands and runtime dispatch routes.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from typing import Any
from unittest.mock import AsyncMock

from mcubridge.protocol import cloud_gateway_pb2, mcu_wire_pb2
from hypothesis import given, strategies as st
import pytest
from dirty_equals import IsPositiveInt, IsStr
from mcubridge.config.settings import RuntimeConfig
from mcubridge.protocol import protocol
from mcubridge.protocol.protocol import Status, build_mcu_dispatch
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
# 1. Declarative Contract Verification (All Dispatch Entries)
# -----------------------------------------------------------------------------


def test_protocol_dispatch_coverage(svc: tuple[BridgeService, RuntimeState, AsyncMock]) -> None:
    """Certifies that all known Operations have defined handlers or default fallbacks."""
    service, _state, _serial = svc
    dispatch = build_mcu_dispatch(service)
    for op in protocol.OPERATIONS:
        if op.mcu_handler:
            assert op.name in dispatch


# -----------------------------------------------------------------------------
# 2. Unsupported Request Fallback Tests
# -----------------------------------------------------------------------------


@pytest.mark.parametrize(("has_serial", "expected_calls"), [(False, 0), (True, 1)])
async def test_unsupported_mcu_request(
    svc: tuple[BridgeService, RuntimeState, AsyncMock],
    has_serial: bool,
    expected_calls: int,
) -> None:
    service, state, serial = svc
    if not has_serial:
        service.serial = None
    else:
        serial.send.return_value = True

    dr_op = protocol.get_operation("rpc.pb.GpioService/DigitalRead")
    assert dr_op is not None
    assert dr_op.service_id is not None
    assert dr_op.method_id is not None
    await service.handle_mcu_frame((dr_op.service_id, dr_op.method_id), 1, b"")
    assert serial.send.await_count == expected_calls
    if has_serial:
        assert state.unknown_command_count == 1
        assert state.unknown_command_last_id == dr_op.method_id
        assert serial.send.call_args[0][0] == Status.UNIMPLEMENTED.value


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
    serial.send.assert_called_with("rpc.pb.MailboxService/Available", mcu_wire_pb2.MailboxAvailableResponse(count=0))

    # With items in queue
    await state.mailbox_queue.append(b"item1")
    assert await on_avail(2, None) is True
    resp = serial.send.call_args[0][1]
    assert resp.count == IsPositiveInt


async def test_mailbox_read_scenarios(svc: tuple[BridgeService, RuntimeState, AsyncMock]) -> None:
    service, state, serial = svc
    on_read: Callable[..., Awaitable[mcu_wire_pb2.MailboxReadResponse]] = service.mailbox_read

    # Without serial
    service.serial = None
    assert (await on_read(1, None)).content == b""

    # Empty queue
    service.serial = serial
    serial.send.return_value = True
    assert (await on_read(1, None)).content == b""
    assert serial.send.call_args[0][0] == "rpc.pb.MailboxService/Read"
    assert serial.send.call_args[0][1].content == b""

    # With queue items
    await state.mailbox_queue.append(b"msg_a")
    await state.mailbox_queue.append(b"msg_b")
    assert (await on_read(2, None)).content == b"msg_a"
    assert serial.send.call_args[0][1].content == b"msg_a"


async def test_mailbox_processed(svc: tuple[BridgeService, RuntimeState, AsyncMock]) -> None:
    service, state, _serial = svc
    captured: list[cloud_gateway_pb2.CloudQueuedPublish] = []

    async def _cap(msg: cloud_gateway_pb2.CloudQueuedPublish, *args: Any, **kwargs: Any) -> None:
        captured.append(msg)

    service.cloud_publisher = _cap
    on_proc: Callable[..., Awaitable[None]] = service.publish_mcu_message
    await on_proc(1, mcu_wire_pb2.MailboxProcessed(message_id=42))
    assert len(captured) == 1
    assert captured[0].topic_name == f"{state.cloud_topic_prefix}/evt/MailboxService/Processed"


# -----------------------------------------------------------------------------
# 4. FileSystem Operations Conformance Suite
# -----------------------------------------------------------------------------


async def test_file_write_scenarios(svc: tuple[BridgeService, RuntimeState, AsyncMock]) -> None:
    service, state, serial = svc
    on_write: Callable[..., Awaitable[cloud_gateway_pb2.GenericResponse]] = service.file_write
    on_remove: Callable[..., Awaitable[cloud_gateway_pb2.GenericResponse]] = service.file_remove

    # Without serial
    service.serial = None
    assert (await on_write(1, mcu_wire_pb2.FileWrite(path="test.txt", data=b"data"))).status == "ok"

    # Path traversal protection
    service.serial = serial
    state.allow_non_tmp_paths = False
    service.config.file_system_root = "/tmp/fs"
    serial.send.return_value = True
    assert (await on_write(1, mcu_wire_pb2.FileWrite(path="../../../etc/shadow", data=b"evil"))).status == "error"
    assert serial.send.call_args[0][0] == Status.INTERNAL.value

    # Successful write
    assert (await on_write(2, mcu_wire_pb2.FileWrite(path="valid.txt", data=b"content"))).status == "ok"
    assert serial.send.call_args[0][0] == Status.OK.value

    # Remove nonexistent file
    service.serial = serial
    assert (await on_remove(1, mcu_wire_pb2.FileRemove(path="missing.txt"))).status == "error"
    assert serial.send.call_args[0][0] == Status.INTERNAL.value

    # Remove existing file
    assert (await on_remove(2, mcu_wire_pb2.FileRemove(path="valid.txt"))).status == "ok"
    assert serial.send.call_args[0][0] == Status.OK.value


# -----------------------------------------------------------------------------
# 5. Process Execution Conformance Suite
# -----------------------------------------------------------------------------


async def test_process_run_async_and_poll(svc: tuple[BridgeService, RuntimeState, AsyncMock]) -> None:
    service, _state, serial = svc
    on_run: Callable[..., Awaitable[mcu_wire_pb2.ProcessRunAsyncResponse]] = service.process_run_async
    on_poll: Callable[..., Awaitable[mcu_wire_pb2.ProcessPollResponse]] = service.process_poll

    # Without serial
    service.serial = None
    assert (await on_run(1, mcu_wire_pb2.ProcessRunAsync(command="rm -rf /"))).pid == 0
    assert (await on_poll(1, mcu_wire_pb2.ProcessPoll(pid=10))).finished is True

    # Disallowed command
    service.serial = serial
    serial.send.return_value = True
    assert (await on_run(1, mcu_wire_pb2.ProcessRunAsync(command="rm -rf /"))).pid == 0
    assert serial.send.call_args[0][0] == Status.INTERNAL.value

    # Allowed command
    assert (await on_run(2, mcu_wire_pb2.ProcessRunAsync(command="echo test_exec"))).pid == IsPositiveInt
    args = serial.send.call_args[0]
    assert args[0] == "rpc.pb.ProcessService/RunAsync"
    pid = args[1].pid
    assert pid == IsPositiveInt

    # Poll process
    assert (await on_poll(3, mcu_wire_pb2.ProcessPoll(pid=pid))).status == Status.OK.value
    assert serial.send.call_args[0][0] == "rpc.pb.ProcessService/Poll"
    await service.process_kill(pid)

    # Disallowed command name fallback
    assert (await on_run(4, mcu_wire_pb2.ProcessRunAsync(command="disallowed_proc_xyz"))).pid == 0


# -----------------------------------------------------------------------------
# 6. Status and Pin Telemetry Conformance Suite
# -----------------------------------------------------------------------------


async def test_status_reporting_and_ack(
    svc: tuple[BridgeService, RuntimeState, AsyncMock],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    service, state, _serial = svc
    captured: list[cloud_gateway_pb2.CloudQueuedPublish] = []

    async def _cap(msg: cloud_gateway_pb2.CloudQueuedPublish, *args: Any, **kwargs: Any) -> None:
        captured.append(msg)

    service.cloud_publisher = _cap
    handle_status: Callable[..., Awaitable[None]] = service.handle_mcu_status

    # Status OK no payload
    await handle_status(Status.OK, 1, b"")
    assert captured[-1].topic_name == f"{state.cloud_topic_prefix}/evt/SystemService/StatusReport"

    # Status with GenericResponse
    await handle_status(Status.INTERNAL, 2, cloud_gateway_pb2.GenericResponse(status="error", message="fault"))
    report = cloud_gateway_pb2.StatusReport.FromString(captured[-1].payload)
    assert report.message == "fault"

    # Status with raw hex
    raw = b"\xde\xad\xbe\xef"
    await handle_status(Status.OK, 4, raw)
    report = cloud_gateway_pb2.StatusReport.FromString(captured[-1].payload)
    assert report.message == IsStr(regex=r"<hex:[0-9a-f]+>")


async def test_pin_telemetry_and_spi(svc: tuple[BridgeService, RuntimeState, AsyncMock]) -> None:
    service, state, mock_serial = svc
    captured: list[cloud_gateway_pb2.CloudQueuedPublish] = []

    async def _cap(msg: cloud_gateway_pb2.CloudQueuedPublish, *args: Any, **kwargs: Any) -> None:
        captured.append(msg)

    service.cloud_publisher = _cap

    # SPI transfer response
    on_spi: Callable[..., Awaitable[None]] = service.publish_mcu_message
    await on_spi(3, mcu_wire_pb2.SpiTransferResponse(data=b"\xaa\x55"))
    assert captured[-1].topic_name == f"{state.cloud_topic_prefix}/res/SpiService/Transfer"
    assert captured[-1].payload == b"\xaa\x55"

    # Datastore put with cache None
    state.datastore_cache = None
    on_ds_put = service.datastore_put
    resp_ds_put = await on_ds_put(1, mcu_wire_pb2.DatastorePut(key="k", value=b"v"))
    assert resp_ds_put.status == "ok"

    # Corrupt Protobuf payload
    state.state = "synchronized"
    state.link_sync_event.set()
    initial_errors = state.serial_decode_errors
    await service.handle_mcu_frame("rpc.pb.DataStoreService/Put", 10, b"\xff\xff\xff\xff")
    assert state.serial_decode_errors == initial_errors + 1
    assert state.serial_decode_errors == IsPositiveInt
    mock_serial.send.assert_awaited_with(Status.INVALID_ARGUMENT.value, b"")


@given(pin_mode=st.from_type(mcu_wire_pb2.PinMode))
def test_pin_mode_roundtrip_property(pin_mode: mcu_wire_pb2.PinMode) -> None:
    """Property: PinMode message preserves fields across protobuf serialization."""
    encoded = pin_mode.SerializeToString()
    decoded = mcu_wire_pb2.PinMode.FromString(encoded)
    assert decoded.pin == pin_mode.pin
    assert decoded.mode == pin_mode.mode


@given(digital_write=st.from_type(mcu_wire_pb2.DigitalWrite))
def test_digital_write_roundtrip_property(digital_write: mcu_wire_pb2.DigitalWrite) -> None:
    """Property: DigitalWrite message preserves fields across protobuf serialization."""
    encoded = digital_write.SerializeToString()
    decoded = mcu_wire_pb2.DigitalWrite.FromString(encoded)
    assert decoded.pin == digital_write.pin
    assert decoded.value == digital_write.value


@given(analog_write=st.from_type(mcu_wire_pb2.AnalogWrite))
def test_analog_write_roundtrip_property(analog_write: mcu_wire_pb2.AnalogWrite) -> None:
    """Property: AnalogWrite message preserves fields across protobuf serialization."""
    encoded = analog_write.SerializeToString()
    decoded = mcu_wire_pb2.AnalogWrite.FromString(encoded)
    assert decoded.pin == analog_write.pin
    assert decoded.value == analog_write.value


@given(datastore_put=st.from_type(mcu_wire_pb2.DatastorePut))
def test_datastore_put_roundtrip_property(datastore_put: mcu_wire_pb2.DatastorePut) -> None:
    """Property: DatastorePut message preserves fields across protobuf serialization."""
    encoded = datastore_put.SerializeToString()
    decoded = mcu_wire_pb2.DatastorePut.FromString(encoded)
    assert decoded.key == datastore_put.key
    assert decoded.value == datastore_put.value


@given(mailbox_push=st.from_type(mcu_wire_pb2.MailboxPush))
def test_mailbox_push_roundtrip_property(mailbox_push: mcu_wire_pb2.MailboxPush) -> None:
    """Property: MailboxPush message preserves fields across protobuf serialization."""
    encoded = mailbox_push.SerializeToString()
    decoded = mcu_wire_pb2.MailboxPush.FromString(encoded)
    assert decoded.data == mailbox_push.data


@given(file_write=st.from_type(mcu_wire_pb2.FileWrite))
def test_file_write_roundtrip_property(file_write: mcu_wire_pb2.FileWrite) -> None:
    """Property: FileWrite message preserves fields across protobuf serialization."""
    encoded = file_write.SerializeToString()
    decoded = mcu_wire_pb2.FileWrite.FromString(encoded)
    assert decoded.path == file_write.path
    assert decoded.data == file_write.data


@given(file_read=st.from_type(cloud_gateway_pb2.FileRead))
def test_file_read_roundtrip_property(file_read: cloud_gateway_pb2.FileRead) -> None:
    """Property: FileRead message preserves fields across protobuf serialization."""
    encoded = file_read.SerializeToString()
    decoded = cloud_gateway_pb2.FileRead.FromString(encoded)
    assert decoded.path == file_read.path


@given(spi_transfer=st.from_type(mcu_wire_pb2.SpiTransfer))
def test_spi_transfer_roundtrip_property(spi_transfer: mcu_wire_pb2.SpiTransfer) -> None:
    """Property: SpiTransfer message preserves fields across protobuf serialization."""
    encoded = spi_transfer.SerializeToString()
    decoded = mcu_wire_pb2.SpiTransfer.FromString(encoded)
    assert decoded.data == spi_transfer.data


@given(pub=st.from_type(cloud_gateway_pb2.CloudQueuedPublish))
def test_cloud_queued_publish_roundtrip_property(pub: cloud_gateway_pb2.CloudQueuedPublish) -> None:
    """Property: CloudQueuedPublish message preserves fields across protobuf serialization."""
    encoded = pub.SerializeToString()
    decoded = cloud_gateway_pb2.CloudQueuedPublish.FromString(encoded)
    assert decoded.topic_name == pub.topic_name
    assert decoded.payload == pub.payload
    assert decoded.qos == pub.qos
    assert decoded.retain == pub.retain
    assert decoded.correlation_data == pub.correlation_data
    assert decoded.response_topic == pub.response_topic
