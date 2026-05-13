import asyncio
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from aiomqtt.message import Message

from mcubridge.transport.mqtt import MqttTransport
from mcubridge.config.settings import RuntimeConfig
from mcubridge.state.context import create_runtime_state


@pytest.fixture
def transport_setup():
    config = RuntimeConfig(mqtt_topic="br", serial_port="/dev/test")
    state = create_runtime_state(config)
    return config, state


@pytest.mark.asyncio
async def test_mqtt_transport_branches_final(transport_setup):
    config, state = transport_setup
    transport = MqttTransport(config, state)
    
    # 1. Test connect_session branches
    with patch("aiomqtt.Client") as mock_client_cls:
        mock_client = AsyncMock()
        mock_client_cls.return_value = mock_client
        
        # Test retry loop with consecutive failures
        mock_client.connect.side_effect = [Exception("fail1"), Exception("fail2"), None]
        
        # We need to bypass the retryer wait to avoid long test
        with patch("tenacity.AsyncRetrying.wraps", lambda x: x):
            try:
                await asyncio.wait_for(transport._connect_session(None), 0.1)
            except (asyncio.TimeoutError, Exception):
                pass
                
    # 2. Test subscriber loop EOF
    mock_client = AsyncMock()
    mock_client.messages = MagicMock()
    async def _mock_iter(self_):
        yield MagicMock(topic="br/test", payload=b"data")
        return # EOF
    mock_client.messages.__aiter__ = _mock_iter
    
    await transport._subscriber_loop(mock_client)
