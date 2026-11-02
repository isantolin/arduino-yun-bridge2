"""Exhaustive tests for mcubridge.transport.serial module. [SIL-2]"""

from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, MagicMock, PropertyMock

import pytest
from mcubridge.config.settings import load_runtime_config
from mcubridge.protocol import protocol
from mcubridge.state.context import RuntimeState
from mcubridge.transport.serial import SerialTransport


@pytest.fixture
def mock_serial_setup() -> tuple[SerialTransport, RuntimeState, MagicMock]:
    config = load_runtime_config({"serial_port": "/dev/ttyMOCK", "serial_baud": 115200})
    state = RuntimeState()
    service = AsyncMock()
    transport = SerialTransport(config, state, service)

    mock_serial = MagicMock()
    mock_serial.baudrate = 115200
    mock_serial.read_until = MagicMock(return_value=b"")
    mock_serial.write = MagicMock(return_value=None)
    transport.serial = mock_serial
    state.serial_writer = mock_serial
    state.serial_tx_allowed.set()

    return transport, state, mock_serial


def test_switch_local_baudrate_failure(mock_serial_setup: tuple[SerialTransport, RuntimeState, MagicMock]) -> None:
    transport, _state, mock_serial = mock_serial_setup
    type(mock_serial).baudrate = PropertyMock(side_effect=OSError("Baudrate error"))

    switch_fn = transport.switch_local_baudrate
    with pytest.raises(RuntimeError, match="UART access failed"):
        switch_fn(9600)


async def test_reset(mock_serial_setup: tuple[SerialTransport, RuntimeState, MagicMock]) -> None:
    transport, _state, _ = mock_serial_setup
    cmd = MagicMock()
    transport.current_command = cmd
    await transport.reset()
    assert transport.current_command is None
    cmd.mark_failure.assert_called_once()


async def test_read_loop_decode_error(
    mock_serial_setup: tuple[SerialTransport, RuntimeState, MagicMock],
) -> None:
    transport, state, mock_serial = mock_serial_setup

    # First call returns invalid HDLC packet + delimiter, second raises CancelledError to end loop
    mock_serial.read_until.side_effect = [
        b"\x7e\xff\xff\x7e",
        asyncio.CancelledError(),
    ]

    read_loop_fn = transport.read_loop
    with pytest.raises(asyncio.CancelledError):
        await read_loop_fn(mock_serial)
    assert state.serial_decode_errors >= 1


async def test_read_loop_crc_error_recovery(
    mock_serial_setup: tuple[SerialTransport, RuntimeState, MagicMock],
) -> None:
    transport, _state, mock_serial = mock_serial_setup

    # Invalid CRC on HDLC frame
    bad_frame = b"\x7e\x01\x03" + b"x" * 20 + b"\x12\x34\x56\x78\x7e"

    mock_serial.read_until.side_effect = [
        bad_frame,
        asyncio.CancelledError(),
    ]

    read_loop_fn = transport.read_loop
    with pytest.raises(asyncio.CancelledError):
        await read_loop_fn(mock_serial)
    assert transport.consecutive_crc_errors == 1


async def test_read_loop_valid_frame_dispatch(
    mock_serial_setup: tuple[SerialTransport, RuntimeState, MagicMock],
) -> None:
    transport, _state, mock_serial = mock_serial_setup

    hdlc_frame = protocol.encode_hdlc_console(b"test_payload")

    mock_serial.read_until.side_effect = [
        hdlc_frame,
        asyncio.CancelledError(),
    ]

    read_loop_fn = transport.read_loop
    with pytest.raises(asyncio.CancelledError):
        await read_loop_fn(mock_serial)
    assert transport.consecutive_crc_errors == 0


async def test_send_raw_success(
    mock_serial_setup: tuple[SerialTransport, RuntimeState, MagicMock],
) -> None:
    transport, _state, mock_serial = mock_serial_setup
    mock_serial.is_open = True

    res = await transport.send_raw(command_id=0x01, payload=b"test")
    assert res is True
    assert mock_serial.write.call_count == 1
    written_data = mock_serial.write.call_args[0][0]
    assert written_data.endswith(protocol.FRAME_DELIMITER)


async def test_send_raw_write_error(
    mock_serial_setup: tuple[SerialTransport, RuntimeState, MagicMock],
) -> None:
    transport, _state, mock_serial = mock_serial_setup
    mock_serial.is_open = True
    mock_serial.write.side_effect = OSError("Write failed")

    res = await transport.send_raw(command_id=0x01, payload=b"test")
    assert res is False
