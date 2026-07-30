"""Exhaustive tests for mcubridge.transport.serial module. [SIL-2]"""

from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, MagicMock, PropertyMock, patch

import pytest
from cobs import cobsr
from mcubridge.config.settings import load_runtime_config
from mcubridge.protocol import protocol
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
    mock_serial.readuntil = AsyncMock(return_value=b"")
    mock_serial.write = AsyncMock(return_value=None)
    mock_serial.drain = AsyncMock(return_value=None)
    transport.serial = mock_serial
    state.serial_writer = mock_serial.transport
    state.serial_tx_allowed.set()

    return transport, state, mock_serial


def test_switch_local_baudrate_failure(mock_serial_setup: tuple[SerialTransport, RuntimeState, MagicMock]) -> None:
    transport, _state, mock_serial = mock_serial_setup
    mock_inner_serial = MagicMock()
    type(mock_inner_serial).baudrate = PropertyMock(side_effect=OSError("Baudrate error"))
    mock_serial.transport.serial = mock_inner_serial

    switch_fn = getattr(transport, "_switch_local_baudrate")
    with pytest.raises(RuntimeError, match="UART access failed"):
        switch_fn(9600)


@pytest.mark.asyncio
async def test_reset(mock_serial_setup: tuple[SerialTransport, RuntimeState, MagicMock]) -> None:
    transport, _state, _ = mock_serial_setup
    cmd = MagicMock()
    setattr(transport, "_current", cmd)
    await transport.reset()
    assert getattr(transport, "_current") is None
    cmd.mark_failure.assert_called_once()


@pytest.mark.asyncio
async def test_read_loop_cobs_decode_error(
    mock_serial_setup: tuple[SerialTransport, RuntimeState, MagicMock],
) -> None:
    transport, _state, mock_serial = mock_serial_setup

    # First call returns invalid COBS packet + delimiter, second raises CancelledError to end loop
    mock_serial.readuntil.side_effect = [
        b"\xff\xff" + protocol.FRAME_DELIMITER,
        asyncio.CancelledError(),
    ]

    read_loop_fn = getattr(transport, "_read_loop")
    with pytest.raises(asyncio.CancelledError):
        await read_loop_fn(mock_serial)


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

    read_loop_fn = getattr(transport, "_read_loop")
    with pytest.raises(asyncio.CancelledError):
        await read_loop_fn(mock_serial)
    assert getattr(transport, "_consecutive_crc_errors") == 1


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

    read_loop_fn = getattr(transport, "_read_loop")
    with pytest.raises(asyncio.CancelledError):
        await read_loop_fn(mock_serial)
    assert getattr(transport, "_consecutive_crc_errors") == 0


@pytest.mark.asyncio
async def test_send_raw_success(
    mock_serial_setup: tuple[SerialTransport, RuntimeState, MagicMock],
) -> None:
    transport, _state, mock_serial = mock_serial_setup
    mock_serial.is_open = True

    res = await transport.send_raw(command_id=0x01, payload=b"test")
    assert res is True
    assert mock_serial.write.call_count == 2


@pytest.mark.asyncio
async def test_send_raw_write_error(
    mock_serial_setup: tuple[SerialTransport, RuntimeState, MagicMock],
) -> None:
    transport, _state, mock_serial = mock_serial_setup
    mock_serial.is_open = True
    mock_serial.write.side_effect = OSError("Write failed")

    res = await transport.send_raw(command_id=0x01, payload=b"test")
    assert res is False
