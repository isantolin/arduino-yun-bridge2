import asyncio
from unittest.mock import AsyncMock, MagicMock, patch
from pathlib import Path

import pytest
from aiomqtt.message import Message

from mcubridge.transport.serial import SerialTransport
from mcubridge.transport.mqtt import MqttTransport
from mcubridge.config.settings import RuntimeConfig
from mcubridge.state.context import create_runtime_state


@pytest.fixture
def transport_setup():
    config = RuntimeConfig(mqtt_topic="br", serial_port="/dev/test")
    state = create_runtime_state(config)
    return config, state


@pytest.mark.asyncio
async def test_serial_transport_logic(transport_setup):
    config, state = transport_setup
    transport = SerialTransport(config, state, service=MagicMock())
    
    # 1. Test _process_packet with valid COBS but invalid Frame
    transport._process_packet(b"\x03\x01\x02\x03\x00") # Malformed
    
    # 2. Test _correlate_frame
    transport._correlate_frame(1, b"payload")
    
    # 3. Test _check_baudrate_fallback threshold
    config.serial_fallback_threshold = 2
    await transport._check_baudrate_fallback()
    await transport._check_baudrate_fallback() # Should trigger fallback
    
    # 4. Test send with closed writer
    assert await transport.send(1, b"") is False


@pytest.mark.asyncio
async def test_mqtt_transport_logic(transport_setup):
    config, state = transport_setup
    transport = MqttTransport(config, state)
    
    # 1. Test _subscriber_loop with specific exceptions
    mock_client = AsyncMock()
    mock_client.messages = MagicMock()
    
    # Corrected iterator: takes self
    async def _mock_iter(self_):
        yield MagicMock(topic="br/test", payload=b"data")
        raise asyncio.CancelledError()
    mock_client.messages.__aiter__ = _mock_iter
    
    with patch.object(transport, "service", AsyncMock()):
        try:
            await transport._subscriber_loop(mock_client)
        except asyncio.CancelledError:
            pass

    # 2. Test get_ssl_context branches
    config.mqtt_tls = True
    config.mqtt_cafile = None
    ctx = config.get_ssl_context()
    assert ctx is not None
