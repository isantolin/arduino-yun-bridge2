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
from mcubridge.router.routers import MQTTRouter
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
        from mcubridge.state.context import PendingPinRequest
        state.pending_digital_reads.append(PendingPinRequest(pin=13, reply_context=None))

        route = TopicRoute(f"br/d/13/{PinAction.READ.value}", "br", Topic.DIGITAL, ("13", PinAction.READ.value))
        result = await comp.handle_mqtt_read(route, make_mqtt_msg(b""))

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
        router = MQTTRouter()
        router.register(Topic.DIGITAL, comp.handle_mqtt_write, action="write")
        router.register(Topic.DIGITAL, comp.handle_mqtt_read, action=PinAction.READ)
        router.register(Topic.DIGITAL, comp.handle_mqtt_mode, action=PinAction.MODE)

        # 1. No segments -> action is None -> not dispatched
        route1 = TopicRoute("br/d", "br", Topic.DIGITAL, ())
        assert not await router.dispatch(route1, make_mqtt_msg(b""))
        
        # 2. Invalid pin (handled inside handler)
        route2 = TopicRoute("br/d/invalid", "br", Topic.DIGITAL, ("invalid",))
        assert await router.dispatch(route2, make_mqtt_msg(b""))
        ctx.serial_flow.send.assert_not_called()
        
        # 3. Unknown subtopic -> action "magic" -> not dispatched
        route3 = TopicRoute("br/d/13/magic", "br", Topic.DIGITAL, ("13", "magic"))
        assert not await router.dispatch(route3, make_mqtt_msg(b""))
        
        # 4. Invalid digital mode (handled inside handler)
        route4 = TopicRoute("br/d/13/mode", "br", Topic.DIGITAL, ("13", "mode"))
        assert await router.dispatch(route4, make_mqtt_msg(b"invalid"))
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
