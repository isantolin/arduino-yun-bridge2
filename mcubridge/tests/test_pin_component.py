"""Unit tests for the PinComponent (SIL-2)."""

from __future__ import annotations

import collections
from typing import Any, cast
from unittest.mock import AsyncMock, MagicMock

import msgspec
import pytest
from aiomqtt.message import Message

from mcubridge.config.settings import RuntimeConfig
from mcubridge.protocol.protocol import Command, PinAction, Topic
from mcubridge.protocol.structures import (
    DigitalReadResponsePacket,
    PinModePacket,
    PinReadPacket,
    TopicRoute,
)
from mcubridge.services.pin import PinComponent
from mcubridge.services.serial_flow import SerialFlowController
from mcubridge.state.context import create_runtime_state


@pytest.fixture
def pin_component(runtime_config: RuntimeConfig) -> PinComponent:
    state = create_runtime_state(runtime_config)
    state.mqtt_topic_prefix = "br"
    
    serial_flow = AsyncMock(spec=SerialFlowController)
    serial_flow.send = AsyncMock(return_value=True)
    enqueue_mqtt = AsyncMock()

    return PinComponent(runtime_config, state, serial_flow, enqueue_mqtt)


@pytest.mark.asyncio
async def test_pin_handle_digital_read_resp(pin_component: PinComponent) -> None:
    from mcubridge.state.context import PendingPinRequest

    pin_component.state.pending_digital_reads.append(
        PendingPinRequest(pin=13, reply_context=None)
    )
    payload = msgspec.msgpack.encode(DigitalReadResponsePacket(value=1))

    await pin_component.handle_digital_read_resp(0, payload)

    pin_component.enqueue_mqtt.assert_called()


@pytest.mark.asyncio
async def test_pin_handle_mqtt_mode(pin_component: PinComponent) -> None:
    route = TopicRoute(
        raw="br/d/13/mode",
        prefix="br",
        topic=Topic.DIGITAL,
        segments=("13", "mode"),
    )
    msg = Message(
        topic="test/topic", payload=b"1", qos=0, retain=False, mid=1, properties=None
    )

    await pin_component.handle_mqtt(route, msg)

    pin_component.serial_flow.send.assert_called_with(
        Command.CMD_SET_PIN_MODE.value,
        msgspec.msgpack.encode(PinModePacket(pin=13, mode=1)),
    )


@pytest.mark.asyncio
async def test_pin_handle_mqtt_read(pin_component: PinComponent) -> None:
    route = TopicRoute(
        raw="br/d/13/read",
        prefix="br",
        topic=Topic.DIGITAL,
        segments=("13", "read"),
    )
    msg = Message(
        topic="test/topic", payload=b"", qos=0, retain=False, mid=1, properties=None
    )

    await pin_component.handle_mqtt(route, msg)

    pin_component.serial_flow.send.assert_called_with(
        Command.CMD_DIGITAL_READ.value,
        msgspec.msgpack.encode(PinReadPacket(pin=13)),
    )
    assert len(pin_component.state.pending_digital_reads) == 1
