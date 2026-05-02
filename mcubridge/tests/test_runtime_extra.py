"""Extra coverage for BridgeService (SIL-2)."""

from __future__ import annotations

from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest

from mcubridge.config.settings import RuntimeConfig
from mcubridge.protocol.topics import Topic
from mcubridge.services.runtime import BridgeService
from mcubridge.state.context import create_runtime_state


@pytest.mark.asyncio
async def test_runtime_publish_bridge_snapshot_handshake(
    runtime_config: RuntimeConfig,
) -> None:
    state = create_runtime_state(runtime_config)
    try:
        enqueue_mqtt = AsyncMock()
        service = BridgeService(runtime_config, state, enqueue_mqtt)
        
        await service._publish_bridge_snapshot("handshake", None)  # type: ignore[reportPrivateUsage]
        enqueue_mqtt.assert_called()
    finally:
        state.cleanup()


@pytest.mark.asyncio
async def test_runtime_reject_topic_action_properties(
    runtime_config: RuntimeConfig,
) -> None:
    state = create_runtime_state(runtime_config)
    try:
        enqueue_mqtt = AsyncMock()
        service = BridgeService(runtime_config, state, enqueue_mqtt)
        
        from aiomqtt.message import Message
        msg = MagicMock(spec=Message)
        msg.topic = "br/d/13"
        msg.properties = None
        
        await service._reject_topic_action(msg, Topic.DIGITAL, "write")  # type: ignore[reportPrivateUsage]
        enqueue_mqtt.assert_called()
    finally:
        state.cleanup()
