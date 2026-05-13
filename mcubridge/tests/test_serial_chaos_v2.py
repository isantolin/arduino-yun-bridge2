import asyncio
from unittest.mock import AsyncMock, MagicMock, patch
from pathlib import Path
import struct

import pytest
from cobs import cobs

from mcubridge.transport.serial import SerialTransport
from mcubridge.config.settings import RuntimeConfig
from mcubridge.state.context import create_runtime_state
from mcubridge.protocol import protocol
from mcubridge.protocol.frame import Frame


@pytest.fixture
def transport_setup():
    config = RuntimeConfig(mqtt_topic="br", serial_port="/dev/test")
    state = create_runtime_state(config)
    return config, state


@pytest.mark.asyncio
async def test_serial_transport_loops_final_v3(transport_setup):
    config, state = transport_setup
    transport = SerialTransport(config, state, service=AsyncMock())
    
    mock_reader = AsyncMock()
    mock_writer = AsyncMock()
    transport.writer = mock_writer 
    
    frame = Frame(command_id=0x01, sequence_id=1, payload=b"ok")
    encoded = cobs.encode(frame.build()) + b"\x00"
    
    mock_reader.read.side_effect = [
        encoded[:2], encoded[2:], 
        b"\xff\x00",              
        b"",                      
    ]
    
    try:
        await asyncio.wait_for(transport._read_loop(mock_reader), 0.1)
    except (asyncio.TimeoutError, Exception):
        pass

    transport._tx_sequence_id = 0xFFFE
    mock_writer.drain = AsyncMock()
    await transport._send_raw(0x01, b"")
    assert transport._tx_sequence_id == 0xFFFF
    await transport._send_raw(0x01, b"")
    assert transport._tx_sequence_id == 0 


@pytest.mark.asyncio
async def test_serial_transport_negotiation_failure_final_v3(transport_setup):
    config, state = transport_setup
    transport = SerialTransport(config, state, service=AsyncMock())
    
    transport._negotiation_future = asyncio.Future()
    async def _mock_wait(fut, timeout):
        if fut and not fut.done():
            pass
        raise asyncio.TimeoutError()
        
    with patch("asyncio.wait_for", _mock_wait):
        res = await transport._negotiate_baudrate(9600)
        assert res is False
    
    if not transport._negotiation_future.done():
        transport._negotiation_future.set_result(None)
