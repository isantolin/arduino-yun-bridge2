"""Extra edge-case tests for PinComponent (SIL-2)."""

from __future__ import annotations

from unittest.mock import AsyncMock

import pytest
from aiomqtt.message import Message
from mcubridge.config.settings import RuntimeConfig
from mcubridge.protocol.topics import Topic, TopicRoute
from mcubridge.services.pin import PinComponent
from mcubridge.services.serial_flow import SerialFlowController
from mcubridge.state.context import create_runtime_state


@pytest.mark.asyncio
async def test_pin_validate_limit(
    runtime_config: RuntimeConfig,
) -> None:
    state = create_runtime_state(runtime_config)
    try:
        serial_flow = AsyncMock(spec=SerialFlowController)
        enqueue_mqtt = AsyncMock()

        comp = PinComponent(runtime_config, state, serial_flow, enqueue_mqtt)

        # 1. Valid pin
        assert comp._validate_pin_limit(13) is True
        # 2. Invalid pin
        assert comp._validate_pin_limit(25) is False
    finally:
        state.cleanup()


@pytest.mark.asyncio
async def test_pin_handle_mqtt_malformed_mode(
    runtime_config: RuntimeConfig,
) -> None:
    state = create_runtime_state(runtime_config)
    try:
        serial_flow = AsyncMock(spec=SerialFlowController)
        enqueue_mqtt = AsyncMock()

        comp = PinComponent(runtime_config, state, serial_flow, enqueue_mqtt)

        # 1. Invalid mode (not 0, 1, 2)
        route = TopicRoute(
            raw="br/d/13/mode",
            prefix="br",
            topic=Topic.DIGITAL,
            segments=("13", "mode"),
        )
        msg = AsyncMock(spec=Message)
        msg.payload = b"5"

        result = await comp.handle_mqtt(route, msg)
        assert result is True
        assert not serial_flow.send.called
    finally:
        state.cleanup()
