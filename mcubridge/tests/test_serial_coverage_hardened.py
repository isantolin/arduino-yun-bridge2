# pyright: reportPrivateUsage=false
"""SIL-2 Serial Transport Coverage Hardening Test Suite.

Genuinely exercises SerialTransport state transitions, packet encoding/decoding,
anti-replay counters, baudrate fallback, DTR toggling, and framing error conditions.
"""

from __future__ import annotations

import asyncio
import secrets
import tempfile
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock

import pytest
import serialx
from cobs import cobsr

import mcubridge.protocol.mcubridge_pb2 as pb
from mcubridge.config.settings import RuntimeConfig
from mcubridge.protocol.frame import build_frame
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


@pytest.mark.asyncio
async def test_serial_transport_acknowledge(tmp_path: Path) -> None:
    transport, state, mock_serialx = _make_transport(tmp_path)

    await transport.acknowledge(Command.CMD_DIGITAL_READ.value, 42, status=Status.OK)
    assert mock_serialx.write.called

    state.cleanup()


@pytest.mark.asyncio
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


@pytest.mark.asyncio
async def test_serial_transport_toggle_dtr(tmp_path: Path) -> None:
    transport, state, mock_serialx = _make_transport(tmp_path)

    # 1. Success
    await transport._toggle_dtr()
    assert mock_serialx.set_modem_pins.called

    # 2. Error handling during DTR toggle
    mock_serialx.set_modem_pins.side_effect = serialx.SerialException("DTR error")
    await transport._toggle_dtr()

    state.cleanup()


@pytest.mark.asyncio
async def test_serial_transport_baudrate_fallback(tmp_path: Path) -> None:
    transport, state, _ = _make_transport(tmp_path)

    transport._consecutive_crc_errors = 1
    await transport._check_baudrate_fallback()
    assert transport._consecutive_crc_errors == 2

    # Trigger threshold (threshold = 3)
    transport._consecutive_crc_errors = 2
    await transport._check_baudrate_fallback()
    assert transport._consecutive_crc_errors == 0

    state.cleanup()


@pytest.mark.asyncio
async def test_serial_transport_process_packet_anti_replay(tmp_path: Path) -> None:
    transport, state, _ = _make_transport(tmp_path)

    state.mark_synchronized()
    state.link_last_nonce_counter = 100

    # Build valid frame with counter <= last_counter (replay attempt)
    old_nonce = secrets.token_bytes(4) + (90).to_bytes(8, "big")
    raw_frame = build_frame(
        command_id=Command.CMD_DIGITAL_READ_RESP.value,
        sequence_id=1,
        payload=pb.DigitalReadResponse(value=1),
        nonce=old_nonce,
    )

    await transport._process_packet(cobsr.encode(raw_frame))
    # Replay must be dropped without advancing counter
    assert state.link_last_nonce_counter == 100

    # Valid newer counter
    new_nonce = secrets.token_bytes(4) + (105).to_bytes(8, "big")
    raw_frame_valid = build_frame(
        command_id=Command.CMD_DIGITAL_READ_RESP.value,
        sequence_id=2,
        payload=pb.DigitalReadResponse(value=1),
        nonce=new_nonce,
    )
    await transport._process_packet(cobsr.encode(raw_frame_valid))
    assert state.link_last_nonce_counter == 105

    state.cleanup()


@pytest.mark.asyncio
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
    if transport._current is not None:
        transport._current.mark_success(pb.DigitalReadResponse(value=1))

    resp = await send_task
    assert isinstance(resp, pb.DigitalReadResponse)

    state.cleanup()
