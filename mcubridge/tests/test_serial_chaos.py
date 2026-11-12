"""Chaos and failure resilience tests for SerialTransport. [SIL-2]"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from unittest.mock import AsyncMock, MagicMock

import serial
from hypothesis import given
from hypothesis import strategies as st
from mcubridge.config.settings import RuntimeConfig
from mcubridge.protocol import protocol
from mcubridge.security.security import LinkCipher
from mcubridge.services.runtime import BridgeService
from mcubridge.state.context import RuntimeState
from mcubridge.transport.serial import SerialTransport


def _make_transport() -> tuple[SerialTransport, RuntimeState]:
    config = RuntimeConfig(
        topic_prefix="br",
        serial_port="/dev/test",
        serial_shared_secret=b"secret1234",
        allow_non_tmp_paths=True,
    )
    state = RuntimeState(
        cloud_topic_prefix="br",
        allow_non_tmp_paths=True,
    )
    transport = SerialTransport(config, state, service=AsyncMock(spec=BridgeService))
    return transport, state


async def test_abrupt_disconnect_os_error() -> None:
    transport, _state = _make_transport()
    mock_serial = MagicMock(spec=serial.Serial)
    mock_serial.read_until.side_effect = OSError("hardware disconnect")

    read_loop: Callable[..., Awaitable[None]] = transport.read_loop
    await read_loop(mock_serial)
    assert mock_serial.read_until.call_count == 1


async def test_negotiation_failure_and_disconnect() -> None:
    transport, state = _make_transport()
    mock_serial = MagicMock(spec=serial.Serial)
    mock_serial.read_until.side_effect = [b"invalid\x00", OSError("disconnect")]

    read_loop: Callable[..., Awaitable[None]] = transport.read_loop
    await read_loop(mock_serial)
    assert not state.is_connected
    assert mock_serial.read_until.call_count == 2


@given(corrupt_payload=st.binary(min_size=1, max_size=32).map(lambda b: b.replace(b"\x00", b"\x01")))
async def test_read_loop_corrupted_frame_resilience(corrupt_payload: bytes) -> None:
    transport, _state = _make_transport()
    mock_serial = MagicMock(spec=serial.Serial)
    mock_serial.is_open = True
    transport.serial = mock_serial

    valid_frame = protocol.encode_hdlc_rpc(0x01, b"ok", call_id=1)
    corrupt_frame = corrupt_payload + protocol.FRAME_DELIMITER

    call_count = 0

    def mock_read(_sep: bytes = protocol.FRAME_DELIMITER, _size: int | None = None) -> bytes:
        nonlocal call_count
        call_count += 1
        if call_count == 1:
            return valid_frame
        if call_count == 2:
            return corrupt_frame
        transport.stop_event.set()
        return b""

    mock_serial.read_until.side_effect = mock_read

    read_loop: Callable[..., Awaitable[None]] = transport.read_loop
    await read_loop(mock_serial)
    assert call_count >= 2
    assert transport.state.serial_decode_errors >= 1


@given(init_seq=st.integers(0, protocol.UINT16_MAX))
async def test_tx_sequence_wrapping_isomorphism(init_seq: int) -> None:
    transport, state = _make_transport()
    mock_serial = MagicMock(spec=serial.Serial)
    mock_serial.is_open = True
    mock_serial.write = MagicMock(return_value=10)
    mock_serial.flush = MagicMock()
    transport.serial = mock_serial
    state.serial_tx_allowed.set()
    state.link_cipher = LinkCipher.for_host(
        transport.config.serial_shared_secret,
        bytes(range(protocol.HANDSHAKE_NONCE_LENGTH)),
    )
    transport.tx_sequence_id = init_seq
    result = await transport.send_raw(0x01, b"")
    assert result is True
    expected_seq = (init_seq + 1) & protocol.UINT16_MAX
    assert transport.tx_sequence_id == expected_seq
    assert mock_serial.write.call_count == 1
