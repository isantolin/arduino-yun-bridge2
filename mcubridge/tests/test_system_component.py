"""Unit tests for the SystemComponent."""

from __future__ import annotations

from typing import Any, cast
from unittest.mock import AsyncMock

import msgspec
import pytest
from mcubridge.config.settings import RuntimeConfig
from mcubridge.protocol.protocol import (
    Command,
    SystemAction,
)
from mcubridge.protocol.structures import (
    FreeMemoryResponsePacket,
    TopicRoute,
    VersionResponsePacket,
)
from mcubridge.protocol.topics import Topic
from mcubridge.services.serial_flow import SerialFlowController
from mcubridge.services.system import SystemComponent
from mcubridge.state.context import RuntimeState
from mcubridge.transport.mqtt import MqttTransport
from aiomqtt.message import Message


@pytest.fixture
def system_component(runtime_config: RuntimeConfig) -> SystemComponent:
    config = runtime_config
    # [SIL-2] Use AsyncMock(spec=Interface) for all component mocks
    state = AsyncMock(spec=RuntimeState)
    state.mqtt_topic_prefix = "br"
    state.mcu_version = None

    serial_flow = AsyncMock(spec=SerialFlowController)
    serial_flow.acknowledge = AsyncMock()
    serial_flow.send = AsyncMock(return_value=True)
    serial_flow.send_and_wait_payload = AsyncMock(return_value=None)
    mqtt_flow = AsyncMock(spec=MqttTransport)
    mqtt_flow.enqueue_mqtt = AsyncMock()

    return SystemComponent(
        config=config, state=state, serial_flow=serial_flow, mqtt_flow=mqtt_flow
    )


@pytest.mark.asyncio
async def test_system_request_mcu_version(system_component: SystemComponent) -> None:
    payload = msgspec.msgpack.encode(VersionResponsePacket(major=1, minor=2, patch=3))
    cast(Any, system_component.serial_flow.send_and_wait_payload).return_value = payload

    await system_component.request_mcu_version()

    cast(Any, system_component.serial_flow.send_and_wait_payload).assert_called_with(
        Command.CMD_GET_VERSION.value, b""
    )
    assert system_component.state.mcu_version == (1, 2, 3)
    cast(Any, system_component.mqtt_flow.enqueue_mqtt).assert_called()


@pytest.mark.asyncio
async def test_system_handle_free_memory_via_mqtt(
    system_component: SystemComponent,
) -> None:
    payload = msgspec.msgpack.encode(FreeMemoryResponsePacket(value=2048))
    cast(Any, system_component.serial_flow.send_and_wait_payload).return_value = payload

    route = TopicRoute(
        raw=f"br/{Topic.SYSTEM}/{SystemAction.FREE_MEMORY.value}/{SystemAction.GET.value}",
        prefix="br",
        topic=Topic.SYSTEM,
        segments=(SystemAction.FREE_MEMORY.value, SystemAction.GET.value),
    )
    msg = AsyncMock(spec=Message)

    await system_component.handle_mqtt(route, msg)

    cast(Any, system_component.serial_flow.send_and_wait_payload).assert_called_with(
        Command.CMD_GET_FREE_MEMORY.value, b""
    )
    cast(Any, system_component.mqtt_flow.enqueue_mqtt).assert_called()


@pytest.mark.asyncio
async def test_system_handle_mqtt_bootloader(system_component: SystemComponent) -> None:
    route = TopicRoute(
        raw=f"br/{Topic.SYSTEM}/{SystemAction.BOOTLOADER.value}",
        prefix="br",
        topic=Topic.SYSTEM,
        segments=(SystemAction.BOOTLOADER.value,),
    )
    msg = Message(
        topic="test/topic", payload=b"", qos=0, retain=False, mid=1, properties=None
    )

    await system_component.handle_mqtt(route, msg)

    cast(Any, system_component.serial_flow.send).assert_called()
    assert (
        cast(Any, system_component.serial_flow.send).call_args[0][0]
        == Command.CMD_ENTER_BOOTLOADER.value
    )
