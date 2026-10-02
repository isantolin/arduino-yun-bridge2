"""Surgical unit test suite for transport/serial.py covering edge paths and error branches."""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import mcubridge.protocol.mcubridge_pb2 as pb
import pytest
import serialx
from cobs import cobsr
from google.protobuf.message import Message as ProtobufMessage
from pytest_mock import MockerFixture
from mcubridge.config.settings import RuntimeConfig
from mcubridge.protocol import protocol
from mcubridge.protocol.frame import build_frame
from mcubridge.protocol.protocol import Command, Status
from mcubridge.state.context import RuntimeState, create_runtime_state
from mcubridge.transport.serial import SerialTransport


def _make_config() -> RuntimeConfig:
    return RuntimeConfig(
        serial_port="/dev/ttyMCU",
        serial_baud=115200,
        serial_safe_baud=9600,
        serial_shared_secret=b"testsharedsecret",
        allow_non_tmp_paths=True,
    )


@pytest.fixture
def mock_config() -> RuntimeConfig:
    return _make_config()


@pytest.fixture
def mock_state(mock_config: RuntimeConfig) -> RuntimeState:
    return create_runtime_state(mock_config)


@pytest.mark.asyncio
async def test_switch_local_baudrate_failure_raises(mock_config: RuntimeConfig, mock_state: RuntimeState) -> None:
    transport = SerialTransport(mock_config, mock_state, None)
    mock_serial = MagicMock()
    # Trigger AttributeError when setting baudrate
    type(mock_serial.transport.serial).baudrate = property(
        fget=lambda self: 9600, fset=MagicMock(side_effect=AttributeError("No baudrate attr"))
    )
    transport.serial = mock_serial

    switch_local_baudrate: Callable[[int], None] = transport.switch_local_baudrate
    with pytest.raises(RuntimeError, match="UART access failed"):
        switch_local_baudrate(115200)


@pytest.mark.asyncio
async def test_toggle_dtr_exception_handled(mock_config: RuntimeConfig, mock_state: RuntimeState) -> None:
    transport = SerialTransport(mock_config, mock_state, None)
    mock_serial = AsyncMock()
    mock_serial.set_modem_pins.side_effect = serialx.SerialException("DTR failed")
    transport.serial = mock_serial

    toggle_dtr: Callable[[], Awaitable[None]] = transport.toggle_dtr
    await toggle_dtr()
    mock_serial.set_modem_pins.assert_awaited_once_with(dtr=False)


@pytest.mark.asyncio
async def test_read_loop_limit_overrun(mock_config: RuntimeConfig, mock_state: RuntimeState) -> None:
    transport = SerialTransport(mock_config, mock_state, None)
    mock_serial = AsyncMock()
    mock_serial.readuntil.side_effect = [
        asyncio.LimitOverrunError("Overrun", 100),
        asyncio.IncompleteReadError(b"partial", expected=10),
    ]
    mock_serial.read.return_value = b""

    read_loop: Callable[..., Awaitable[None]] = transport.read_loop
    await read_loop(mock_serial)
    assert mock_state.serial_decode_errors == 1


@pytest.mark.asyncio
async def test_read_loop_generic_exception(mock_config: RuntimeConfig, mock_state: RuntimeState) -> None:
    transport = SerialTransport(mock_config, mock_state, None)
    mock_serial = AsyncMock()
    mock_serial.readuntil.side_effect = OSError("Read hardware error")

    read_loop: Callable[..., Awaitable[None]] = transport.read_loop
    await read_loop(mock_serial)
    mock_serial.readuntil.assert_awaited_once_with(protocol.FRAME_DELIMITER)


@pytest.mark.asyncio
async def test_process_packet_baudrate_negotiation_response(
    mock_config: RuntimeConfig, mock_state: RuntimeState
) -> None:
    transport = SerialTransport(mock_config, mock_state, None)
    mock_serial = MagicMock()
    transport.serial = mock_serial
    transport.is_negotiating = True
    fut: asyncio.Future[bool] = asyncio.Future()
    transport.negotiation_future = fut

    raw = cobsr.encode(build_frame(Command.CMD_SET_BAUDRATE_RESP.value, 1))

    process_packet: Callable[[bytes], Awaitable[None]] = transport.process_packet
    await process_packet(raw)
    assert fut.done()
    assert fut.result() is True
    assert mock_serial.transport.serial.baudrate == 115200


@pytest.mark.asyncio
async def test_correlate_frame_ack_with_protobuf_payload(mock_config: RuntimeConfig, mock_state: RuntimeState) -> None:
    transport = SerialTransport(mock_config, mock_state, None)
    pending = MagicMock()
    pending.command_id = Command.CMD_FILE_WRITE.value
    pending.expected_resp_ids = set()
    pending.success = None
    transport.current_command = pending

    # ACK payload for CMD_FILE_WRITE
    ack = pb.AckPacket(command_id=Command.CMD_FILE_WRITE.value)
    correlate_frame: Callable[..., None] = transport.correlate_frame
    correlate_frame(Status.ACK.value, ack)

    pending.mark_success.assert_called_once_with(ack)


@pytest.mark.asyncio
async def test_correlate_frame_ack_with_invalid_bytes(mock_config: RuntimeConfig, mock_state: RuntimeState) -> None:
    transport = SerialTransport(mock_config, mock_state, None)
    pending = MagicMock()
    pending.command_id = Command.CMD_FILE_WRITE.value
    pending.expected_resp_ids = set()
    pending.success = None
    transport.current_command = pending

    # Corrupted ACK payload (invalid protobuf bytes)
    correlate_frame: Callable[..., None] = transport.correlate_frame
    correlate_frame(Status.ACK.value, b"\xff\xff\xff\xff")
    assert pending.ack_received is True
    pending.mark_success.assert_called_once_with(b"\xff\xff\xff\xff")


@pytest.mark.asyncio
async def test_stop_sets_event_and_closes_serial(mock_config: RuntimeConfig, mock_state: RuntimeState) -> None:
    transport = SerialTransport(mock_config, mock_state, None)
    mock_serial = AsyncMock()
    transport.serial = mock_serial

    await transport.stop()

    stop_event = transport.stop_event
    assert stop_event.is_set()
    mock_serial.close.assert_awaited_once()


@pytest.mark.asyncio
async def test_reset_marks_failure(mock_config: RuntimeConfig, mock_state: RuntimeState) -> None:
    transport = SerialTransport(mock_config, mock_state, None)
    pending = MagicMock()
    transport.current_command = pending

    await transport.reset()

    pending.mark_failure.assert_called_once_with(Status.TIMEOUT.value)
    assert transport.current_command is None


@pytest.mark.asyncio
async def test_send_raw_no_serial(mock_config: RuntimeConfig, mock_state: RuntimeState) -> None:
    transport = SerialTransport(mock_config, mock_state, None)
    transport.serial = None
    ok = await transport.send_raw(Command.CMD_GET_VERSION.value, b"")
    assert ok is False


@pytest.mark.asyncio
async def test_check_baudrate_fallback_triggers(mock_config: RuntimeConfig, mock_state: RuntimeState) -> None:
    transport = SerialTransport(mock_config, mock_state, None)
    transport.consecutive_crc_errors = mock_config.serial_fallback_threshold - 1

    check_fallback: Callable[[], Awaitable[None]] = transport.check_baudrate_fallback
    await check_fallback()
    assert transport.consecutive_crc_errors == 0


@pytest.mark.asyncio
async def test_correlate_frame_failure_status(mock_config: RuntimeConfig, mock_state: RuntimeState) -> None:
    from mcubridge.protocol.structures import PendingCommand

    transport = SerialTransport(mock_config, mock_state, None)
    pending = PendingCommand(
        command_id=Command.CMD_FILE_READ.value, expected_resp_ids=[Command.CMD_FILE_READ_RESP.value]
    )
    transport.current_command = pending

    # Response to request matching
    correlate_frame: Callable[..., None] = transport.correlate_frame
    correlate_frame(Command.CMD_FILE_READ_RESP.value, b"content")
    assert pending.completion.is_set()
    assert pending.success is True
    assert pending.response_payload == b"content"


@pytest.mark.asyncio
async def test_connect_without_runner_fn(mock_config: RuntimeConfig, mock_state: RuntimeState) -> None:
    transport = SerialTransport(mock_config, mock_state, None)
    mock_run = AsyncMock()
    transport.connect_and_run = mock_run
    await transport.connect()
    mock_run.assert_awaited_once()


@pytest.mark.asyncio
async def test_logging_not_debug_branches(
    mock_config: RuntimeConfig, mock_state: RuntimeState, mocker: MockerFixture
) -> None:
    transport = SerialTransport(mock_config, mock_state, None)
    mock_serial = AsyncMock()
    transport.serial = mock_serial

    mocker.patch("mcubridge.transport.serial.logger.is_enabled_for", return_value=False)
    # 1. send_raw when logger is not debug
    ok = await transport.send_raw(Command.CMD_GET_VERSION.value, b"")
    assert ok is True

    # 2. process_packet when logger is not debug
    payload = pb.VersionResponse(major=2, minor=8, patch=8)
    encoded = cobsr.encode(build_frame(Command.CMD_GET_VERSION_RESP.value, 1, payload.SerializeToString()))
    await transport.process_packet(encoded)
    assert mock_state.metrics.serial_bytes_received.value > 0


@pytest.mark.asyncio
async def test_process_packet_uninitialized_protobuf(
    mock_config: RuntimeConfig, mock_state: RuntimeState, mocker: MockerFixture
) -> None:
    transport = SerialTransport(mock_config, mock_state, None)
    mock_proto = MagicMock(spec=ProtobufMessage)
    mock_proto.IsInitialized.return_value = False

    mocker_frame = MagicMock()
    mocker_frame.envelope.command_id = Command.CMD_GET_VERSION.value
    mocker_frame.envelope.sequence_id = 1
    mocker_frame.envelope.nonce = b"\x00" * 12
    mocker_frame.payload = mock_proto

    mocker.patch("mcubridge.transport.serial.parse_frame", return_value=mocker_frame)
    initial_errors = mock_state.serial_decode_errors
    await transport.process_packet(b"\x00" * 20)
    assert mock_state.serial_decode_errors == initial_errors + 1


@pytest.mark.asyncio
async def test_connect_and_run_negotiation_failure(
    mock_config: RuntimeConfig, mock_state: RuntimeState, mocker: MockerFixture
) -> None:
    mock_config.serial_baud = 115200
    mock_config.serial_safe_baud = 9600
    mock_config.serial_port = "/dev/ttyS0"
    transport = SerialTransport(mock_config, mock_state, None)

    mock_serial_obj = AsyncMock()
    mock_serial_obj.transport = MagicMock()

    class _MockAsyncSerial:
        def __init__(self, *args: Any, **kwargs: Any) -> None:
            pass

        async def __aenter__(self) -> Any:
            return mock_serial_obj

        async def __aexit__(self, *args: Any) -> None:
            pass

    mocker.patch("serialx.AsyncSerial", _MockAsyncSerial)
    mocker.patch.object(transport, "toggle_dtr", new=AsyncMock())
    mocker.patch.object(transport, "negotiate_baudrate", new=AsyncMock(return_value=False))
    with pytest.raises(ConnectionError, match="Baudrate negotiation failed"):
        await transport.connect_and_run()
