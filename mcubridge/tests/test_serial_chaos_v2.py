import pytest
import asyncio
from unittest.mock import AsyncMock
from mcubridge.transport.serial import SerialTransport
from mcubridge.protocol.frame import build_frame
from cobs import cobsr
from typing import Any
from mcubridge.services.runtime import BridgeService


@pytest.fixture
def transport_setup():
    from mcubridge.protocol.structures import RuntimeConfig
    from mcubridge.state.context import create_runtime_state

    config = RuntimeConfig(topic_prefix="br", serial_port="/dev/test")
    state = create_runtime_state(config)
    return config, state


@pytest.mark.asyncio
async def test_serial_transport_loops_final_v3(transport_setup: Any) -> None:
    config, state = transport_setup
    transport = SerialTransport(config, state, service=AsyncMock(spec=BridgeService))

    import serialx

    mock_serial = AsyncMock(spec=serialx.AsyncSerial)
    mock_serial.is_open = True
    transport.serial = mock_serial

    frame_bytes = build_frame(command_id=0x01, sequence_id=1, payload=b"ok")
    encoded = cobsr.encode(frame_bytes) + b"\x00"

    call_count = 0

    async def readuntil_mock_impl(sep: bytes = b"\x00") -> bytes:
        nonlocal call_count
        call_count += 1
        if call_count == 1:
            return encoded
        if call_count == 2:
            return b"\xff\x00"
        await asyncio.sleep(2)
        raise asyncio.CancelledError()

    mock_serial.readuntil.side_effect = readuntil_mock_impl

    try:
        await asyncio.wait_for(getattr(transport, "_read_loop")(mock_serial), 0.1)
    except TimeoutError:
        pass

    import itertools

    setattr(transport, "_tx_sequence_counter", itertools.count(0xFFFE))
    # send_raw consumes 0xFFFE (65534 & 0xFFFF = 65534)
    await transport.send_raw(0x01, b"")
    # Next value from counter is 65535; the one after that wraps: 65536 & 0xFFFF = 0
    consumed_next = next(__import__("typing").cast("Any", getattr(transport, "_tx_sequence_counter")))
    assert consumed_next == 0xFFFF  # 65535 — counter advanced correctly


@pytest.mark.asyncio
async def test_serial_transport_negotiation_failure_final_v3(transport_setup: Any) -> None:
    config, state = transport_setup
    transport = SerialTransport(config, state, service=AsyncMock(spec=BridgeService))
    import serialx

    mock_serial = AsyncMock(spec=serialx.AsyncSerial)
    mock_serial.readuntil.side_effect = [b"invalid\x00", asyncio.IncompleteReadError(b"", None)]
    await getattr(transport, "_read_loop")(mock_serial)
    assert not state.is_connected
