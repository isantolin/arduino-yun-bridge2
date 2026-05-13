import asyncio
from typing import Any, Tuple
from unittest.mock import AsyncMock, patch

import pytest

# pyright: reportPrivateUsage=false
from mcubridge.transport.serial import SerialTransport
from mcubridge.transport.mqtt import MqttTransport
from mcubridge.config.settings import RuntimeConfig
from mcubridge.state.context import create_runtime_state, RuntimeState


@pytest.fixture
def transport_setup() -> Tuple[RuntimeConfig, RuntimeState]:
    config = RuntimeConfig(mqtt_topic="br", serial_port="/dev/test")
    state = create_runtime_state(config)
    return config, state


@pytest.mark.asyncio
async def test_serial_transport_chaos(transport_setup: Any) -> None:
    config, state = transport_setup
    transport = SerialTransport(config, state, service=AsyncMock())

    transport._process_packet(b"\x01\x02\x03")
    transport._correlate_frame(1, b"payload")

    config.serial_fallback_threshold = 2
    await transport._check_baudrate_fallback()
    await transport._check_baudrate_fallback()

    assert await transport.send(1, b"") is False


@pytest.mark.asyncio
async def test_mqtt_transport_chaos(transport_setup: Any) -> None:
    config, state = transport_setup
    transport = MqttTransport(config, state)

    async def _mock_iter(self_: Any) -> Any:
        yield AsyncMock(topic="br/test", payload=b"data")
        raise asyncio.CancelledError()

    mock_client = AsyncMock()
    mock_client.messages.__aiter__ = _mock_iter

    with patch.object(transport, "service", AsyncMock()):
        try:
            await transport._subscriber_loop(mock_client)
        except asyncio.CancelledError:
            pass

    config.mqtt_tls = True
    config.mqtt_cafile = None
    ctx = config.get_ssl_context()
    assert ctx is not None
