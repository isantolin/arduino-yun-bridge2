import asyncio
from typing import Any, Tuple
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

# pyright: reportPrivateUsage=false
from mcubridge.transport.mqtt import MqttTransport
from mcubridge.config.settings import RuntimeConfig
from mcubridge.state.context import create_runtime_state, RuntimeState


@pytest.fixture
def transport_setup() -> Tuple[RuntimeConfig, RuntimeState]:
    config = RuntimeConfig(mqtt_topic="br", serial_port="/dev/test")
    state = create_runtime_state(config)
    return config, state


@pytest.mark.asyncio
async def test_mqtt_transport_branches_final(transport_setup: Any) -> None:
    config, state = transport_setup
    transport = MqttTransport(config, state)

    with patch("aiomqtt.Client") as mock_client_cls:
        mock_client = AsyncMock()
        mock_client_cls.return_value = mock_client

        mock_client.connect.side_effect = [Exception("fail1"), Exception("fail2"), None]

        def dummy_wraps(x: Any) -> Any:
            return x

        with patch("tenacity.AsyncRetrying.wraps", dummy_wraps):
            try:
                await asyncio.wait_for(transport._connect_session(None), 0.1)
            except (asyncio.TimeoutError, Exception):
                pass

    mock_client = AsyncMock()
    mock_client.messages = MagicMock()

    async def _mock_iter(self_: Any) -> Any:
        yield MagicMock(topic="br/test", payload=b"data")
        return  # EOF

    mock_client.messages.__aiter__ = _mock_iter

    await transport._subscriber_loop(mock_client)
