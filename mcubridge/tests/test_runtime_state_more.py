"""Extra tests for RuntimeState edges and MQTT logic."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from mcubridge.state.context import create_runtime_state
from mcubridge.transport.mqtt import MqttTransport


@pytest.mark.asyncio
async def test_stash_mqtt_message_shim_delegates(runtime_config):
    state = create_runtime_state(runtime_config)
    transport = MqttTransport(runtime_config, state)
    with patch(
        "mcubridge.mqtt.spool_manager.stash_message", new_callable=AsyncMock
    ) as mock_stash:
        await transport.stash_mqtt_message(MagicMock())
        assert mock_stash.called


@pytest.mark.asyncio
async def test_flush_mqtt_spool_shim_delegates(runtime_config):
    state = create_runtime_state(runtime_config)
    transport = MqttTransport(runtime_config, state)
    with patch(
        "mcubridge.mqtt.spool_manager.flush_spool", new_callable=AsyncMock
    ) as mock_flush:
        await transport.flush_mqtt_spool()
        assert mock_flush.called
