"""Extra edge-case tests for PinComponent (SIL-2)."""

from __future__ import annotations
from mcubridge.services.serial_flow import SerialFlowController
from mcubridge.transport.mqtt import MqttTransport

from aiomqtt.message import Message
import os
import time
from unittest.mock import AsyncMock, MagicMock

import pytest
from mcubridge.config.settings import RuntimeConfig
from mcubridge.protocol.topics import Topic, TopicRoute
from mcubridge.protocol.protocol import PinAction
from mcubridge.services.pin import PinComponent
from mcubridge.state.context import create_runtime_state


@pytest.mark.asyncio
async def test_pin_handle_read_overflow() -> None:
    config = RuntimeConfig(
        serial_shared_secret=b"secret_1234",
        file_system_root=os.path.abspath(
            f".tmp_tests/mcubridge-test-{os.getpid()}-{time.time_ns()}"
        ),
        allow_non_tmp_paths=True,
    )
    state = create_runtime_state(config)
    try:
        state.pending_pin_request_limit = 1
        serial_flow = AsyncMock(spec=SerialFlowController)
        serial_flow.send = AsyncMock(return_value=True)
        mqtt_flow = AsyncMock(spec=MqttTransport)
        mqtt_flow.enqueue_mqtt = AsyncMock()

        comp = PinComponent(config, state, serial_flow, mqtt_flow)

        # Fill queue
        state.pending_digital_reads.append(MagicMock())

        route = TopicRoute(
            f"br/d/13/{PinAction.READ.value}",
            "br",
            Topic.DIGITAL,
            ("13", PinAction.READ.value),
        )
        result = await comp.handle_mqtt(
            route,
            Message(
                topic="test/topic",
                payload=b"",
                qos=0,
                retain=False,
                mid=1,
                properties=None,
            ),
        )

        # True because handled (rejected gracefully)
        assert result is True
        # Should publish overflow error
        mqtt_flow.enqueue_mqtt.assert_called_once()
        args, kwargs = mqtt_flow.enqueue_mqtt.call_args
        msg = args[0] if args else kwargs.get("message")
        props = msg.user_properties
        assert ("bridge-error", "pending-pin-overflow") in props
    finally:
        state.cleanup()


@pytest.mark.asyncio
async def test_pin_handle_mqtt_edge_cases() -> None:
    config = RuntimeConfig(
        serial_shared_secret=b"secret_1234",
        file_system_root=os.path.abspath(
            f".tmp_tests/mcubridge-test-{os.getpid()}-{time.time_ns()}"
        ),
        allow_non_tmp_paths=True,
    )
    state = create_runtime_state(config)
    try:
        serial_flow = AsyncMock(spec=SerialFlowController)
        serial_flow.send = AsyncMock(return_value=True)
        mqtt_flow = AsyncMock(spec=MqttTransport)
        mqtt_flow.enqueue_mqtt = AsyncMock()

        comp = PinComponent(config, state, serial_flow, mqtt_flow)

        # 1. No segments
        route1 = TopicRoute("br/d", "br", Topic.DIGITAL, ())
        await comp.handle_mqtt(
            route1,
            Message(
                topic="test/topic",
                payload=b"",
                qos=0,
                retain=False,
                mid=1,
                properties=None,
            ),
        )
        serial_flow.send.assert_not_called()

        # 2. Invalid pin
        route2 = TopicRoute("br/d/invalid", "br", Topic.DIGITAL, ("invalid",))
        await comp.handle_mqtt(
            route2,
            Message(
                topic="test/topic",
                payload=b"",
                qos=0,
                retain=False,
                mid=1,
                properties=None,
            ),
        )
        serial_flow.send.assert_not_called()

        # 3. Unknown subtopic
        route3 = TopicRoute("br/d/13/magic", "br", Topic.DIGITAL, ("13", "magic"))
        await comp.handle_mqtt(
            route3,
            Message(
                topic="test/topic",
                payload=b"",
                qos=0,
                retain=False,
                mid=1,
                properties=None,
            ),
        )
        serial_flow.send.assert_not_called()

        # 4. Invalid mode
        route4 = TopicRoute("br/d/13/mode", "br", Topic.DIGITAL, ("13", "mode"))
        await comp.handle_mqtt(
            route4,
            Message(
                topic="test/topic",
                payload=b"invalid",
                qos=0,
                retain=False,
                mid=1,
                properties=None,
            ),
        )
        serial_flow.send.assert_not_called()

        await comp.handle_mqtt(
            route4,
            Message(
                topic="test/topic",
                payload=b"99",
                qos=0,
                retain=False,
                mid=1,
                properties=None,
            ),
        )
        serial_flow.send.assert_not_called()

        # 5. Invalid write value
        route5 = TopicRoute("br/d/13", "br", Topic.DIGITAL, ("13",))
        await comp.handle_mqtt(
            route5,
            Message(
                topic="test/topic",
                payload=b"invalid",
                qos=0,
                retain=False,
                mid=1,
                properties=None,
            ),
        )
        serial_flow.send.assert_not_called()

    finally:
        state.cleanup()


@pytest.mark.asyncio
async def test_pin_handle_analog_read_resp_malformed() -> None:
    config = RuntimeConfig(
        serial_shared_secret=b"secret_1234",
        file_system_root=os.path.abspath(
            f".tmp_tests/mcubridge-test-{os.getpid()}-{time.time_ns()}"
        ),
        allow_non_tmp_paths=True,
    )
    state = create_runtime_state(config)
    try:
        serial_flow = AsyncMock(spec=SerialFlowController)
        mqtt_flow = AsyncMock(spec=MqttTransport)
        mqtt_flow.enqueue_mqtt = AsyncMock()

        comp = PinComponent(config, state, serial_flow, mqtt_flow)

        import msgspec
        from mcubridge.protocol.structures import AnalogReadResponsePacket

        payload = msgspec.msgpack.encode(AnalogReadResponsePacket(value=1023))
        await comp.handle_analog_read_resp(0, payload)

        mqtt_flow.enqueue_mqtt.assert_called_once()
    finally:
        state.cleanup()
