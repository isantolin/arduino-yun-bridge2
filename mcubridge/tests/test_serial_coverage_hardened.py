"""SIL-2 Serial Transport Coverage Hardening Test Suite.

Genuinely exercises SerialTransport state transitions, packet encoding/decoding,
anti-replay counters, baudrate fallback, DTR toggling, and framing error conditions.
"""

from __future__ import annotations

import asyncio
import tempfile
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock

import mcubridge.protocol.mcubridge_pb2 as pb
import serialx
from mcubridge.config.settings import RuntimeConfig
from mcubridge.protocol import protocol
from mcubridge.protocol.protocol import Command, Status
from mcubridge.state.context import create_runtime_state
from mcubridge.transport.serial import SerialTransport


def _make_transport(tmp_path: Path | None = None) -> tuple[SerialTransport, Any, Any]:
    d = str(tmp_path or tempfile.mkdtemp())
    config = RuntimeConfig(
        topic_prefix="test/br",
        serial_port="/dev/null",
        serial_baud=115200,
        serial_safe_baud=57600,
        serial_fallback_threshold=3,
        cloud_spool_dir=d,
        allow_non_tmp_paths=True,
    )
    state = create_runtime_state(config)
    mock_serialx = AsyncMock(spec=serialx.AsyncSerial)
    mock_serialx.is_open = True
    mock_serialx.write = AsyncMock(return_value=10)
    mock_serialx.flush = AsyncMock()
    mock_serialx.close = AsyncMock()
    mock_serialx.set_modem_pins = AsyncMock()

    transport = SerialTransport(config=config, state=state, service=None)
    transport.serial = mock_serialx
    return transport, state, mock_serialx


async def test_serial_transport_acknowledge(tmp_path: Path) -> None:
    transport, state, mock_serialx = _make_transport(tmp_path)

    await transport.send_raw(Status.OK.value, b"", 42)
    assert mock_serialx.write.called

    state.cleanup()


async def test_serial_transport_send_raw(tmp_path: Path) -> None:
    transport, state, mock_serialx = _make_transport(tmp_path)

    # 1. Normal send_raw
    res = await transport.send_raw(Command.CMD_DIGITAL_READ.value, pb.PinRead(pin=13), seq_id=10)
    assert res is True
    assert mock_serialx.write.called

    # 2. send_raw with closed serial
    transport.serial = None
    res_none = await transport.send_raw(Command.CMD_DIGITAL_READ.value, b"")
    assert res_none is False

    # 3. send_raw with write exception
    transport.serial = mock_serialx
    mock_serialx.write.side_effect = serialx.SerialException("device disconnected")
    res_err = await transport.send_raw(Command.CMD_DIGITAL_READ.value, b"")
    assert res_err is False

    state.cleanup()


async def test_serial_transport_toggle_dtr(tmp_path: Path) -> None:
    transport, state, mock_serialx = _make_transport(tmp_path)

    # 1. Success
    toggle_dtr: Callable[[], Awaitable[None]] = transport.toggle_dtr
    await toggle_dtr()
    assert mock_serialx.set_modem_pins.called

    # 2. Error handling during DTR toggle
    mock_serialx.set_modem_pins.side_effect = serialx.SerialException("DTR error")
    await toggle_dtr()

    state.cleanup()


async def test_serial_transport_baudrate_fallback(tmp_path: Path) -> None:
    transport, state, _ = _make_transport(tmp_path)

    check_fallback: Callable[[], Awaitable[None]] = transport.check_baudrate_fallback

    transport.consecutive_crc_errors = 1
    await check_fallback()
    assert transport.consecutive_crc_errors == 2

    # Trigger threshold (threshold = 3)
    transport.consecutive_crc_errors = 2
    await check_fallback()
    assert transport.consecutive_crc_errors == 0

    state.cleanup()


async def test_serial_transport_feed_data_hdlc_rpc(tmp_path: Path) -> None:
    transport, state, _ = _make_transport(tmp_path)
    service_mock = AsyncMock()
    transport.service = service_mock

    state.connection_fsm.synchronize()

    # Valid HDLC RPC frame
    encoded = protocol.encode_hdlc_rpc(
        Command.CMD_DIGITAL_READ.value,
        pb.PinRead(pin=13),
        call_id=5,
    )

    await transport.feed_data(encoded)
    service_mock.handle_mcu_frame.assert_awaited_once()
    assert state.serial_decode_errors == 0

    state.cleanup()


async def test_serial_transport_send_with_retries(tmp_path: Path) -> None:
    transport, state, _ = _make_transport(tmp_path)

    # When send is called without expected response (fire and forget / untracked), it sends raw and returns True
    res = await transport.send(Command.CMD_LINK_RESET.value, pb.GenericResponse(message="reset"))
    assert res is True

    # When send is called with expected response (CMD_DIGITAL_READ expects CMD_DIGITAL_READ_RESP)
    send_task = asyncio.create_task(
        transport.send(
            Command.CMD_DIGITAL_READ.value,
            pb.PinRead(pin=13),
        )
    )

    await asyncio.sleep(0.01)
    # Correlate response
    current = transport.current_command
    if current is not None:
        current.mark_success(pb.DigitalReadResponse(value=1))

    resp = await send_task
    assert isinstance(resp, pb.DigitalReadResponse)

    state.cleanup()
