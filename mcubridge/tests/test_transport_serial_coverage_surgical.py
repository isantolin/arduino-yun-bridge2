# pyright: reportPrivateUsage=false
"""Surgical coverage tests for SerialTransport in transport/serial.py. [SIL-2]"""

from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
import serialx

from mcubridge.config.settings import RuntimeConfig
from mcubridge.protocol import mcubridge_pb2 as pb
from mcubridge.protocol.protocol import Command, Status
from mcubridge.state.context import create_runtime_state
from mcubridge.transport.serial import SerialTransport


@pytest.fixture
def serial_transport_setup(tmp_path: object) -> tuple[SerialTransport, object, RuntimeConfig]:
    config = RuntimeConfig(
        serial_port="/dev/null",
        serial_baud=115200,
        serial_safe_baud=9600,
        serial_shared_secret=b"testsecret123456",
        file_system_root=str(tmp_path),
        allow_non_tmp_paths=True,
    )
    state = create_runtime_state(config)
    transport = SerialTransport(config, state, None)
    return transport, state, config


@pytest.mark.asyncio
async def test_switch_local_baudrate_success(
    serial_transport_setup: tuple[SerialTransport, object, RuntimeConfig],
) -> None:
    transport, _state, _config = serial_transport_setup
    mock_serial = MagicMock()
    transport.serial = mock_serial
    transport._switch_local_baudrate(115200)
    assert mock_serial.transport.serial.baudrate == 115200


@pytest.mark.asyncio
async def test_switch_local_baudrate_failure_raises(
    serial_transport_setup: tuple[SerialTransport, object, RuntimeConfig],
) -> None:
    transport, _state, _config = serial_transport_setup
    mock_serial = MagicMock()
    # Accessing .baudrate raises OSError
    type(mock_serial.transport.serial).baudrate = property(fset=MagicMock(side_effect=OSError("UART err")))
    transport.serial = mock_serial
    with pytest.raises(RuntimeError, match="UART access failed"):
        transport._switch_local_baudrate(115200)


@pytest.mark.asyncio
async def test_reset_marks_failure(
    serial_transport_setup: tuple[SerialTransport, object, RuntimeConfig],
) -> None:
    transport, _state, _config = serial_transport_setup
    from mcubridge.protocol.structures import PendingCommand

    pending = PendingCommand(command_id=Command.CMD_GET_VERSION.value)
    transport._current = pending
    await transport.reset()
    assert pending.failure_status == Status.TIMEOUT.value
    assert transport._current is None


@pytest.mark.asyncio
async def test_toggle_dtr_exception_handled(
    serial_transport_setup: tuple[SerialTransport, object, RuntimeConfig],
) -> None:
    transport, _state, _config = serial_transport_setup
    mock_serial = AsyncMock()
    mock_serial.set_modem_pins.side_effect = serialx.SerialException("DTR fail")
    transport.serial = mock_serial
    # Does not raise, catches exception and logs
    await transport._toggle_dtr()


@pytest.mark.asyncio
async def test_stop_sets_event_and_closes_serial(
    serial_transport_setup: tuple[SerialTransport, object, RuntimeConfig],
) -> None:
    transport, _state, _config = serial_transport_setup
    mock_serial = AsyncMock()
    transport.serial = mock_serial
    await transport.stop()
    assert transport._stop_event.is_set()
    mock_serial.close.assert_called_once()


@pytest.mark.asyncio
async def test_read_loop_limit_overrun(
    serial_transport_setup: tuple[SerialTransport, object, RuntimeConfig],
) -> None:
    transport, _state, _config = serial_transport_setup
    mock_serial = AsyncMock()
    # First call LimitOverrunError, second call IncompleteReadError to exit loop
    mock_serial.readuntil.side_effect = [
        asyncio.LimitOverrunError("overrun", 100),
        asyncio.IncompleteReadError(b"", 10),
    ]
    await transport._read_loop(mock_serial)
    assert transport.state.serial_decode_errors == 1


@pytest.mark.asyncio
async def test_read_loop_generic_exception(
    serial_transport_setup: tuple[SerialTransport, object, RuntimeConfig],
) -> None:
    transport, _state, _config = serial_transport_setup
    mock_serial = AsyncMock()
    mock_serial.readuntil.side_effect = OSError("Read failed")
    await transport._read_loop(mock_serial)


@pytest.mark.asyncio
async def test_correlate_frame_ack_with_protobuf_payload(
    serial_transport_setup: tuple[SerialTransport, object, RuntimeConfig],
) -> None:
    transport, _state, _config = serial_transport_setup
    from mcubridge.protocol.structures import PendingCommand

    pending = PendingCommand(command_id=Command.CMD_GET_VERSION.value)
    transport._current = pending

    ack_payload = pb.AckPacket(command_id=Command.CMD_GET_VERSION.value)
    transport._correlate_frame(Status.ACK.value, ack_payload)
    assert pending.ack_received is True


@pytest.mark.asyncio
async def test_correlate_frame_ack_with_invalid_bytes(
    serial_transport_setup: tuple[SerialTransport, object, RuntimeConfig],
) -> None:
    transport, _state, _config = serial_transport_setup
    from mcubridge.protocol.structures import PendingCommand

    pending = PendingCommand(command_id=Command.CMD_GET_VERSION.value)
    transport._current = pending

    # Invalid bytes decode failure handled gracefully
    transport._correlate_frame(Status.ACK.value, b"\xff\xff\xff")


@pytest.mark.asyncio
async def test_process_packet_baudrate_negotiation_response(
    serial_transport_setup: tuple[SerialTransport, object, RuntimeConfig],
) -> None:
    transport, _state, _config = serial_transport_setup
    transport._negotiating = True
    fut = asyncio.get_event_loop().create_future()
    transport._negotiation_future = fut

    with (
        patch("mcubridge.transport.serial.cobsr.decode", return_value=b"decoded"),
        patch("mcubridge.transport.serial.parse_frame") as mock_parse,
        patch.object(transport, "_switch_local_baudrate") as mock_switch,
    ):
        mock_env = MagicMock()
        mock_env.command_id = Command.CMD_SET_BAUDRATE_RESP.value
        mock_env.sequence_id = 1
        mock_parse.return_value = MagicMock(envelope=mock_env, payload=b"")

        await transport._process_packet(b"encoded")
        mock_switch.assert_called_once_with(115200)
        assert fut.result() is True
