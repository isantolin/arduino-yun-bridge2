"""Surgical unit test suite for transport/serial.py covering edge paths and error branches."""

from __future__ import annotations

from collections.abc import Awaitable, Callable
import asyncio
from unittest.mock import AsyncMock, MagicMock

import pytest
from pytest_mock import MockerFixture
import serialx

from mcubridge.config.settings import RuntimeConfig
from cobs import cobsr

import mcubridge.protocol.mcubridge_pb2 as pb
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

    switch_local_baudrate: Callable[[int], None] = getattr(transport, "_switch_local_baudrate")
    with pytest.raises(RuntimeError, match="UART access failed"):
        switch_local_baudrate(115200)


@pytest.mark.asyncio
async def test_toggle_dtr_exception_handled(mock_config: RuntimeConfig, mock_state: RuntimeState) -> None:
    transport = SerialTransport(mock_config, mock_state, None)
    mock_serial = AsyncMock()
    mock_serial.set_modem_pins.side_effect = serialx.SerialException("DTR failed")
    transport.serial = mock_serial

    # Should not raise exception
    toggle_dtr: Callable[[], Awaitable[None]] = getattr(transport, "_toggle_dtr")
    await toggle_dtr()


@pytest.mark.asyncio
async def test_read_loop_limit_overrun(mock_config: RuntimeConfig, mock_state: RuntimeState) -> None:
    transport = SerialTransport(mock_config, mock_state, None)
    mock_serial = AsyncMock()
    mock_serial.readuntil.side_effect = [
        asyncio.LimitOverrunError("Overrun", 100),
        asyncio.IncompleteReadError(b"partial", expected=10),
    ]
    mock_serial.read.return_value = b""

    read_loop: Callable[[object], Awaitable[None]] = getattr(transport, "_read_loop")
    await read_loop(mock_serial)
    assert mock_state.serial_decode_errors == 1


@pytest.mark.asyncio
async def test_read_loop_generic_exception(mock_config: RuntimeConfig, mock_state: RuntimeState) -> None:
    transport = SerialTransport(mock_config, mock_state, None)
    mock_serial = AsyncMock()
    mock_serial.readuntil.side_effect = OSError("Read hardware error")

    read_loop: Callable[[object], Awaitable[None]] = getattr(transport, "_read_loop")
    await read_loop(mock_serial)


@pytest.mark.asyncio
async def test_process_packet_baudrate_negotiation_response(
    mock_config: RuntimeConfig, mock_state: RuntimeState, mocker: MockerFixture
) -> None:
    transport = SerialTransport(mock_config, mock_state, None)
    setattr(transport, "_negotiating", True)
    fut: asyncio.Future[bool] = asyncio.Future()
    setattr(transport, "_negotiation_future", fut)

    raw = cobsr.encode(build_frame(Command.CMD_SET_BAUDRATE_RESP.value, 1))

    mock_switch = mocker.patch.object(transport, "_switch_local_baudrate")
    process_packet: Callable[[bytes], Awaitable[None]] = getattr(transport, "_process_packet")
    await process_packet(raw)
    assert fut.done()
    assert fut.result() is True
    mock_switch.assert_called_once_with(115200)


@pytest.mark.asyncio
async def test_correlate_frame_ack_with_protobuf_payload(mock_config: RuntimeConfig, mock_state: RuntimeState) -> None:
    transport = SerialTransport(mock_config, mock_state, None)
    pending = MagicMock()
    pending.command_id = Command.CMD_FILE_WRITE.value
    pending.expected_resp_ids = set()
    pending.success = None
    setattr(transport, "_current", pending)

    # ACK payload for CMD_FILE_WRITE
    ack = pb.AckPacket(command_id=Command.CMD_FILE_WRITE.value)
    correlate_frame: Callable[[int, object], None] = getattr(transport, "_correlate_frame")
    correlate_frame(Status.ACK.value, ack)

    pending.mark_success.assert_called_once_with(ack)


@pytest.mark.asyncio
async def test_correlate_frame_ack_with_invalid_bytes(mock_config: RuntimeConfig, mock_state: RuntimeState) -> None:
    transport = SerialTransport(mock_config, mock_state, None)
    pending = MagicMock()
    pending.command_id = Command.CMD_FILE_WRITE.value
    setattr(transport, "_current", pending)

    # Corrupted ACK payload (invalid protobuf bytes)
    correlate_frame: Callable[[int, object], None] = getattr(transport, "_correlate_frame")
    correlate_frame(Status.ACK.value, b"\xff\xff\xff\xff")
    # Should not raise exception


@pytest.mark.asyncio
async def test_stop_sets_event_and_closes_serial(mock_config: RuntimeConfig, mock_state: RuntimeState) -> None:
    transport = SerialTransport(mock_config, mock_state, None)
    mock_serial = AsyncMock()
    transport.serial = mock_serial

    await transport.stop()

    stop_event = getattr(transport, "_stop_event")
    assert stop_event.is_set()
    mock_serial.close.assert_awaited_once()


@pytest.mark.asyncio
async def test_reset_marks_failure(mock_config: RuntimeConfig, mock_state: RuntimeState) -> None:
    transport = SerialTransport(mock_config, mock_state, None)
    pending = MagicMock()
    setattr(transport, "_current", pending)

    await transport.reset()

    pending.mark_failure.assert_called_once_with(Status.TIMEOUT.value)
    assert getattr(transport, "_current") is None


@pytest.mark.asyncio
async def test_send_raw_no_serial(mock_config: RuntimeConfig, mock_state: RuntimeState) -> None:
    transport = SerialTransport(mock_config, mock_state, None)
    transport.serial = None
    ok = await transport.send_raw(Command.CMD_GET_VERSION.value, b"")
    assert ok is False


@pytest.mark.asyncio
async def test_check_baudrate_fallback_triggers(
    mock_config: RuntimeConfig, mock_state: RuntimeState, mocker: MockerFixture
) -> None:
    transport = SerialTransport(mock_config, mock_state, None)
    setattr(transport, "_consecutive_crc_errors", mock_config.serial_fallback_threshold - 1)

    mock_neg = mocker.patch.object(transport, "_negotiate_baudrate", new_callable=AsyncMock)
    mock_neg.return_value = True
    check_fallback: Callable[[], Awaitable[None]] = getattr(transport, "_check_baudrate_fallback")
    await check_fallback()
    assert getattr(transport, "_consecutive_crc_errors") == 0
    mock_neg.assert_awaited_once_with(mock_config.serial_safe_baud)


@pytest.mark.asyncio
async def test_correlate_frame_failure_status(mock_config: RuntimeConfig, mock_state: RuntimeState) -> None:
    from mcubridge.protocol.structures import PendingCommand

    transport = SerialTransport(mock_config, mock_state, None)
    pending = PendingCommand(
        command_id=Command.CMD_FILE_READ.value, expected_resp_ids=[Command.CMD_FILE_READ_RESP.value]
    )
    setattr(transport, "_current", pending)

    # Response to request matching
    correlate_frame: Callable[[int, object], None] = getattr(transport, "_correlate_frame")
    correlate_frame(Command.CMD_FILE_READ_RESP.value, b"content")
    assert pending.completion.is_set()
    assert pending.success is True
    assert pending.response_payload == b"content"
