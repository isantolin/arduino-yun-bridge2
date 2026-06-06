import pytest
import asyncio
from unittest.mock import MagicMock, AsyncMock
from mcubridge.transport.serial import SerialTransport
from mcubridge.protocol.frame import build_frame
from cobs import cobs
from typing import Any
from mcubridge.services.runtime import BridgeService


@pytest.fixture
def transport_setup():
    from mcubridge.protocol.structures import RuntimeConfig
    from mcubridge.state.context import create_runtime_state

    config = RuntimeConfig(mqtt_topic="br", serial_port="/dev/test")
    state = create_runtime_state(config)
    return config, state


@pytest.mark.asyncio
async def test_serial_transport_loops_final_v3(transport_setup: Any) -> None:
    config, state = transport_setup
    transport = SerialTransport(config, state, service=MagicMock(spec=BridgeService))

    mock_reader = MagicMock(spec=asyncio.StreamReader)
    mock_reader.feed_eof = __import__("unittest").mock.Mock()
    mock_writer = MagicMock(spec=asyncio.StreamWriter)
    mock_writer.write = __import__("unittest").mock.Mock()
    mock_writer.close = __import__("unittest").mock.Mock()
    transport.writer = mock_writer

    frame_bytes = build_frame(command_id=0x01, sequence_id=1, payload=b"ok")
    encoded = cobs.encode(frame_bytes) + b"\x00"

    call_count = 0

    async def read_mock_impl(n: int = -1) -> bytes:
        nonlocal call_count
        call_count += 1
        if call_count == 1:
            return encoded[:2]
        if call_count == 2:
            return encoded[2:]
        if call_count == 3:
            return b"\xff\x00"
        await asyncio.sleep(2)
        return b""

    mock_reader.read.side_effect = read_mock_impl

    try:
        await asyncio.wait_for(getattr(transport, "_read_loop")(mock_reader), 0.1)
    except TimeoutError:
        pass

    setattr(transport, "_tx_sequence_id", 0xFFFE)
    mock_writer.drain = AsyncMock()
    await transport.send_raw(0x01, b"")
    assert getattr(transport, "_tx_sequence_id") == 65535


@pytest.mark.asyncio
async def test_serial_transport_negotiation_failure_final_v3(transport_setup: Any) -> None:
    config, state = transport_setup
    transport = SerialTransport(config, state, service=MagicMock(spec=BridgeService))
    mock_reader = MagicMock(spec=asyncio.StreamReader)
    mock_reader.read.side_effect = [b"invalid", b""]
    await getattr(transport, "_read_loop")(mock_reader)
    assert state.is_connected is False
