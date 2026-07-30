"""Exhaustive tests for mcubridge.transport.serial module. [SIL-2]"""

from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from cobs import cobsr
from mcubridge.config.settings import load_runtime_config
from mcubridge.protocol import mcubridge_pb2 as pb, protocol
from mcubridge.protocol.frame import build_frame
from mcubridge.state.context import RuntimeState
from mcubridge.transport.serial import SerialTransport


@pytest.fixture
def mock_serial_setup() -> tuple[SerialTransport, RuntimeState, MagicMock]:
    config = load_runtime_config({"serial_port": "/dev/ttyMOCK", "serial_baud": 115200})
    state = RuntimeState()
    service = AsyncMock()
    transport = SerialTransport(config, state, service)

    mock_serial = MagicMock()
    mock_serial.transport.serial.baudrate = 115200
    mock_serial.readuntil = AsyncMock()
    mock_serial.write = AsyncMock()
    transport.serial = mock_serial
    state.serial_writer = mock_serial.transport

    return transport, state, mock_serial


def test_switch_local_baudrate_failure(mock_serial_setup: tuple[SerialTransport, RuntimeState, MagicMock]) -> None:
    transport, _state, mock_serial = mock_serial_setup
    type(mock_serial.transport.serial).baudrate = property(
        fget=lambda self: 115200, fset=MagicMock(side_effect=OSError("Baudrate error"))
    )
    with pytest.raises(RuntimeError, match="UART access failed"):
        transport._switch_local_baudrate(9600)


@pytest.mark.asyncio
async def test_reset(mock_serial_setup: tuple[SerialTransport, RuntimeState, MagicMock]) -> None:
    transport, _state, _ = mock_serial_setup
    cmd = MagicMock()
    transport._current = cmd
    await transport.reset()
    assert transport._current is None
    cmd.mark_failure.assert_called_once()


@pytest.mark.asyncio
async def test_read_loop_cobs_decode_error(
    mock_serial_setup: tuple[SerialTransport, RuntimeState, MagicMock],
) -> None:
    transport, _state, mock_serial = mock_serial_setup

    # First call returns invalid COBS packet + delimiter, second raises CancelledError to end loop
    mock_serial.readuntil.side_effect = [
        b"\xFF\xFF" + protocol.FRAME_DELIMITER,
        asyncio.CancelledError(),
    ]

    with pytest.raises(asyncio.CancelledError):
        await transport._read_loop(mock_serial)


@pytest.mark.asyncio
async def test_read_loop_crc_error_recovery(
    mock_serial_setup: tuple[SerialTransport, RuntimeState, MagicMock],
) -> None:
    transport, _state, mock_serial = mock_serial_setup

    # Valid COBS, invalid CRC
    raw_body = b"invalid_frame_body"
    encoded = cobsr.encode(raw_body) + protocol.FRAME_DELIMITER

    mock_serial.readuntil.side_effect = [
        encoded,
        asyncio.CancelledError(),
    ]

    with pytest.raises(asyncio.CancelledError):
        await transport._read_loop(mock_serial)
    assert transport._consecutive_crc_errors == 1


@pytest.mark.asyncio
async def test_read_loop_valid_frame_dispatch(
    mock_serial_setup: tuple[SerialTransport, RuntimeState, MagicMock],
) -> None:
    transport, _state, mock_serial = mock_serial_setup

    frame_bytes = build_frame(command_id=0x01, sequence_id=1, payload=b"test_payload")
    cobs_frame = cobsr.encode(frame_bytes) + protocol.FRAME_DELIMITER

    mock_serial.readuntil.side_effect = [
        cobs_frame,
        asyncio.CancelledError(),
    ]

    with pytest.raises(asyncio.CancelledError):
        await transport._read_loop(mock_serial)
    assert transport._consecutive_crc_errors == 0


@pytest.mark.asyncio
async def test_write_frame_raw_success(
    mock_serial_setup: tuple[SerialTransport, RuntimeState, MagicMock],
) -> None:
    transport, _state, mock_serial = mock_serial_setup

    res = await transport.write_frame_raw(command_id=0x01, payload=b"test")
    assert res is True
    mock_serial.write.assert_called_once()


@pytest.mark.asyncio
async def test_write_frame_raw_write_error(
    mock_serial_setup: tuple[SerialTransport, RuntimeState, MagicMock],
) -> None:
    transport, _state, mock_serial = mock_serial_setup
    mock_serial.write.side_effect = OSError("Write failed")

    res = await transport.write_frame_raw(command_id=0x01, payload=b"test")
    assert res is False
