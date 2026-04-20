"""Extra edge-case tests for PinComponent (SIL-2)."""

from __future__ import annotations

import os
import time
from unittest.mock import AsyncMock, MagicMock

import pytest
from mcubridge.config.settings import RuntimeConfig
from mcubridge.protocol.topics import Topic, TopicRoute
from mcubridge.protocol.protocol import PinAction
from mcubridge.services.pin import PinComponent
from mcubridge.state.context import create_runtime_state
from tests._helpers import make_mqtt_msg


@pytest.mark.asyncio
async def test_pin_handle_read_overflow() -> None:
    config = RuntimeConfig(
        serial_shared_secret=b"secret_1234",
        file_system_root=f"/tmp/mcubridge-test-{os.getpid()}-{time.time_ns()}",
    )
    state = create_runtime_state(config)
    try:
        state.pending_pin_request_limit = 1
        ctx = MagicMock()
        ctx.serial_flow = MagicMock()
        ctx.serial_flow.send = AsyncMock(return_value=True)
        ctx.mqtt_flow = MagicMock()
        ctx.mqtt_flow.publish = AsyncMock()

        comp = PinComponent(config, state, ctx)

        # Fill queue
        state.pending_digital_reads.append(MagicMock())

        route = TopicRoute(f"br/d/13/{PinAction.READ.value}", "br", Topic.DIGITAL, ("13", PinAction.READ.value))
        result = await comp.handle_mqtt(route, make_mqtt_msg(b""))

        # True because handled (rejected gracefully)
        assert result is True
        # Should publish overflow error
        ctx.mqtt_flow.publish.assert_called_once()
        args, kwargs = ctx.mqtt_flow.publish.call_args
        props = kwargs.get("properties") or args[4]
        assert ("bridge-error", "pending-pin-overflow") in props
    finally:
        state.cleanup()


@pytest.mark.asyncio
async def test_pin_handle_mqtt_edge_cases() -> None:
    config = RuntimeConfig(
        serial_shared_secret=b"secret_1234",
        file_system_root=f"/tmp/mcubridge-test-{os.getpid()}-{time.time_ns()}",
    )
    state = create_runtime_state(config)
    try:
        ctx = MagicMock()
        ctx.serial_flow = MagicMock()
        ctx.serial_flow.send = AsyncMock(return_value=True)
        ctx.mqtt_flow = MagicMock()
        ctx.mqtt_flow.publish = AsyncMock()

        comp = PinComponent(config, state, ctx)

        # 1. No segments
        route1 = TopicRoute("br/d", "br", Topic.DIGITAL, ())
        await comp.handle_mqtt(route1, make_mqtt_msg(b""))
        ctx.serial_flow.send.assert_not_called()

        # 2. Invalid pin
        route2 = TopicRoute("br/d/invalid", "br", Topic.DIGITAL, ("invalid",))
        await comp.handle_mqtt(route2, make_mqtt_msg(b""))
        ctx.serial_flow.send.assert_not_called()

        # 3. Unknown subtopic
        route3 = TopicRoute("br/d/13/magic", "br", Topic.DIGITAL, ("13", "magic"))
        await comp.handle_mqtt(route3, make_mqtt_msg(b""))
        ctx.serial_flow.send.assert_not_called()

        # 4. Invalid mode
        route4 = TopicRoute("br/d/13/mode", "br", Topic.DIGITAL, ("13", "mode"))
        await comp.handle_mqtt(route4, make_mqtt_msg(b"invalid"))
        ctx.serial_flow.send.assert_not_called()

        await comp.handle_mqtt(route4, make_mqtt_msg(b"99"))
        ctx.serial_flow.send.assert_not_called()

        # 5. Invalid write value
        route5 = TopicRoute("br/d/13", "br", Topic.DIGITAL, ("13",))
        await comp.handle_mqtt(route5, make_mqtt_msg(b"invalid"))
        ctx.serial_flow.send.assert_not_called()

    finally:
        state.cleanup()


@pytest.mark.asyncio
async def test_pin_handle_analog_read_resp_malformed() -> None:
    config = RuntimeConfig(
        serial_shared_secret=b"secret_1234",
        file_system_root=f"/tmp/mcubridge-test-{os.getpid()}-{time.time_ns()}",
    )
    state = create_runtime_state(config)
    try:
        ctx = MagicMock()
        ctx.serial_flow = MagicMock()
        ctx.mqtt_flow = MagicMock()
        ctx.mqtt_flow.publish = AsyncMock()

        comp = PinComponent(config, state, ctx)

        await comp.handle_analog_read_resp(0, b"\xff\xff")

        ctx.mqtt_flow.publish.assert_not_called()
    finally:
        state.cleanup()
