# pyright: reportPrivateUsage=false
"""Surgical unit test suite for transport/serial.py covering edge paths and error branches."""

from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
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

    with pytest.raises(RuntimeError, match="UART access failed"):
        transport._switch_local_baudrate(115200)


@pytest.mark.asyncio
async def test_toggle_dtr_exception_handled(mock_config: RuntimeConfig, mock_state: RuntimeState) -> None:
    transport = SerialTransport(mock_config, mock_state, None)
    mock_serial = AsyncMock()
    mock_serial.set_modem_pins.side_effect = serialx.SerialException("DTR failed")
    transport.serial = mock_serial

    # Should not raise exception
    await transport._toggle_dtr()


@pytest.mark.asyncio
async def test_read_loop_limit_overrun(mock_config: RuntimeConfig, mock_state: RuntimeState) -> None:
    transport = SerialTransport(mock_config, mock_state, None)
    mock_serial = AsyncMock()
    mock_serial.readuntil.side_effect = [
        asyncio.LimitOverrunError("Overrun", 100),
        asyncio.IncompleteReadError(b"partial", expected=10),
    ]
    mock_serial.read.return_value = b""

    await transport._read_loop(mock_serial)
    assert mock_state.serial_decode_errors == 1


@pytest.mark.asyncio
async def test_read_loop_generic_exception(mock_config: RuntimeConfig, mock_state: RuntimeState) -> None:
    transport = SerialTransport(mock_config, mock_state, None)
    mock_serial = AsyncMock()
    mock_serial.readuntil.side_effect = OSError("Read hardware error")

    await transport._read_loop(mock_serial)


@pytest.mark.asyncio
async def test_process_packet_baudrate_negotiation_response(
    mock_config: RuntimeConfig, mock_state: RuntimeState
) -> None:
    transport = SerialTransport(mock_config, mock_state, None)
    transport._negotiating = True
    fut: asyncio.Future[bool] = asyncio.Future()
    transport._negotiation_future = fut

    raw = cobsr.encode(build_frame(Command.CMD_SET_BAUDRATE_RESP.value, 1))

    with patch.object(transport, "_switch_local_baudrate") as mock_switch:
        await transport._process_packet(raw)
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
    transport._current = pending

    # ACK payload for CMD_FILE_WRITE
    ack = pb.AckPacket(command_id=Command.CMD_FILE_WRITE.value)
    transport._correlate_frame(Status.ACK.value, ack)

    pending.mark_success.assert_called_once_with(ack)


@pytest.mark.asyncio
async def test_correlate_frame_ack_with_invalid_bytes(mock_config: RuntimeConfig, mock_state: RuntimeState) -> None:
    transport = SerialTransport(mock_config, mock_state, None)
    pending = MagicMock()
    pending.command_id = Command.CMD_FILE_WRITE.value
    transport._current = pending

    # Corrupted ACK payload (invalid protobuf bytes)
    transport._correlate_frame(Status.ACK.value, b"\xff\xff\xff\xff")
    # Should not raise exception


@pytest.mark.asyncio
async def test_stop_sets_event_and_closes_serial(mock_config: RuntimeConfig, mock_state: RuntimeState) -> None:
    transport = SerialTransport(mock_config, mock_state, None)
    mock_serial = AsyncMock()
    transport.serial = mock_serial

    await transport.stop()

    assert transport._stop_event.is_set()
    mock_serial.close.assert_awaited_once()


@pytest.mark.asyncio
async def test_reset_marks_failure(mock_config: RuntimeConfig, mock_state: RuntimeState) -> None:
    transport = SerialTransport(mock_config, mock_state, None)
    pending = MagicMock()
    transport._current = pending

    await transport.reset()

    pending.mark_failure.assert_called_once_with(Status.TIMEOUT.value)
    assert transport._current is None
