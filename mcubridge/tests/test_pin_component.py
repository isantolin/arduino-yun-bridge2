"""Unit tests for the PinComponent."""

from __future__ import annotations

import collections
from typing import Any, cast
from unittest.mock import AsyncMock

import msgspec
import pytest
from mcubridge.config.settings import RuntimeConfig
from mcubridge.protocol.protocol import (
    Command,
    PinAction,
)
from mcubridge.protocol.structures import (
    DigitalReadResponsePacket,
    PinModePacket,
    PinReadPacket,
    TopicRoute,
)
from mcubridge.protocol.topics import Topic
from mcubridge.services.pin import PinComponent
from mcubridge.services.serial_flow import SerialFlowController
from mcubridge.state.context import RuntimeState
from mcubridge.transport.mqtt import MqttTransport
from aiomqtt.message import Message


@pytest.fixture
def pin_component(runtime_config: RuntimeConfig) -> PinComponent:
    config = runtime_config
    # [SIL-2] Use AsyncMock(spec=Interface) for all component mocks
    state = AsyncMock(spec=RuntimeState)
    state.mqtt_topic_prefix = "br"
    state.pending_digital_reads = collections.deque()
    state.pending_analog_reads = collections.deque()
    state.pending_pin_request_limit = 10
    state.mcu_capabilities = None

    serial_flow = AsyncMock(spec=SerialFlowController)
    serial_flow.acknowledge = AsyncMock()
    serial_flow.send = AsyncMock(return_value=True)
    mqtt_flow = AsyncMock(spec=MqttTransport)
    mqtt_flow.enqueue_mqtt = AsyncMock()

    return PinComponent(
        config=config, state=state, serial_flow=serial_flow, mqtt_flow=mqtt_flow
    )


@pytest.mark.asyncio
async def test_pin_handle_digital_read_resp(pin_component: PinComponent) -> None:
    from mcubridge.state.context import PendingPinRequest

    pin_component.state.pending_digital_reads.append(
        PendingPinRequest(pin=13, reply_context=None)
    )
    payload = msgspec.msgpack.encode(DigitalReadResponsePacket(value=1))

    await pin_component.handle_digital_read_resp(0, payload)

    cast(Any, pin_component.mqtt_flow.enqueue_mqtt).assert_called()


@pytest.mark.asyncio
async def test_pin_handle_mqtt_mode(pin_component: PinComponent) -> None:
    route = TopicRoute(
        raw=f"br/{Topic.DIGITAL}/13/{PinAction.MODE.value}",
        prefix="br",
        topic=Topic.DIGITAL,
        segments=("13", PinAction.MODE.value),
    )
    msg = Message(
        topic="test/topic", payload=b"1", qos=0, retain=False, mid=1, properties=None
    )  # OUTPUT

    await pin_component.handle_mqtt(route, msg)

    cast(Any, pin_component.serial_flow.send).assert_called_with(
        Command.CMD_SET_PIN_MODE.value,
        msgspec.msgpack.encode(PinModePacket(pin=13, mode=1)),
    )


@pytest.mark.asyncio
async def test_pin_handle_mqtt_read(pin_component: PinComponent) -> None:
    route = TopicRoute(
        raw=f"br/{Topic.DIGITAL}/13/{PinAction.READ.value}",
        prefix="br",
        topic=Topic.DIGITAL,
        segments=("13", PinAction.READ.value),
    )
    msg = Message(
        topic="test/topic", payload=b"1", qos=0, retain=False, mid=1, properties=None
    )

    await pin_component.handle_mqtt(route, msg)

    cast(Any, pin_component.serial_flow.send).assert_called_with(
        Command.CMD_DIGITAL_READ.value,
        msgspec.msgpack.encode(PinReadPacket(pin=13)),
    )
    assert len(pin_component.state.pending_digital_reads) == 1
