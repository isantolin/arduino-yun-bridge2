"""Chaos and failure resilience tests for SerialTransport. [SIL-2]"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from typing import Any
from unittest.mock import AsyncMock

import pytest
from cobs import cobsr
from hypothesis import given
from hypothesis import strategies as st
from mcubridge.config.settings import RuntimeConfig
from mcubridge.protocol import protocol
from mcubridge.protocol.frame import build_frame
from mcubridge.services.runtime import BridgeService
from mcubridge.state.context import create_runtime_state
from mcubridge.transport.serial import SerialTransport


def _make_transport() -> tuple[SerialTransport, Any]:
    config = RuntimeConfig(
        topic_prefix="br",
        serial_port="/dev/test",
        serial_shared_secret=b"secret1234",
        allow_non_tmp_paths=True,
    )
    state = create_runtime_state(config)
    transport = SerialTransport(config, state, service=AsyncMock(spec=BridgeService))
    return transport, state


@pytest.mark.asyncio
async def test_abrupt_disconnect_os_error() -> None:
    transport, _state = _make_transport()
    mock_serial = AsyncMock()
    mock_serial.readuntil.side_effect = OSError("hardware disconnect")

    read_loop: Callable[[object], Awaitable[None]] = getattr(transport, "_read_loop")
    await read_loop(mock_serial)
    assert mock_serial.readuntil.call_count == 1


@pytest.mark.asyncio
async def test_negotiation_failure_and_disconnect() -> None:
    transport, state = _make_transport()
    mock_serial = AsyncMock()
    mock_serial.readuntil.side_effect = [b"invalid\x00", asyncio.IncompleteReadError(b"", None)]

    read_loop: Callable[[object], Awaitable[None]] = getattr(transport, "_read_loop")
    await read_loop(mock_serial)
    assert not state.is_connected


@pytest.mark.asyncio
@given(corrupt_payload=st.binary(min_size=1, max_size=32).map(lambda b: b.replace(b"\x00", b"\x01")))
async def test_read_loop_corrupted_frame_resilience(corrupt_payload: bytes) -> None:
    transport, _state = _make_transport()
    mock_serial = AsyncMock()
    mock_serial.is_open = True
    transport.serial = mock_serial

    valid_frame = cobsr.encode(build_frame(command_id=0x01, sequence_id=1, payload=b"ok")) + protocol.FRAME_DELIMITER
    corrupt_frame = corrupt_payload + protocol.FRAME_DELIMITER

    call_count = 0

    async def mock_read(_sep: bytes = protocol.FRAME_DELIMITER) -> bytes:
        nonlocal call_count
        call_count += 1
        if call_count == 1:
            return valid_frame
        if call_count == 2:
            return corrupt_frame
        await asyncio.sleep(2)
        raise asyncio.CancelledError()

    mock_serial.readuntil.side_effect = mock_read

    read_loop: Callable[[object], Awaitable[None]] = getattr(transport, "_read_loop")
    with pytest.raises(TimeoutError):
        await asyncio.wait_for(read_loop(mock_serial), 0.05)


@pytest.mark.asyncio
@given(init_seq=st.integers(0, protocol.UINT16_MAX))
async def test_tx_sequence_wrapping_isomorphism(init_seq: int) -> None:
    transport, state = _make_transport()
    mock_serial = AsyncMock()
    mock_serial.is_open = True
    transport.serial = mock_serial
    state.serial_tx_allowed.set()
    setattr(transport, "_tx_sequence_id", init_seq)
    result = await transport.send_raw(0x01, b"")
    assert result is True
    expected_seq = (init_seq + 1) & protocol.UINT16_MAX
    assert getattr(transport, "_tx_sequence_id") == expected_seq
    assert mock_serial.write.call_count == 1
