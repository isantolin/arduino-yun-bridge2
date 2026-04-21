"""Unit tests for the SystemComponent."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import msgspec
import pytest
from mcubridge.protocol.protocol import (
    Command,
    SystemAction,
)
from mcubridge.protocol.structures import (
    FreeMemoryResponsePacket,
    VersionResponsePacket,
)
from mcubridge.protocol.topics import Topic
from mcubridge.services.serial_flow import SerialFlowController
from mcubridge.services.system import SystemComponent
from mcubridge.state.context import RuntimeState
from mcubridge.transport.mqtt import MqttTransport
from tests._helpers import make_mqtt_msg, make_route, make_test_config


@pytest.fixture
def system_component() -> SystemComponent:
    config = make_test_config()
    state = MagicMock(spec=RuntimeState)
    state.mqtt_topic_prefix = "br"
    state.mcu_version = None

    serial_flow = MagicMock(spec=SerialFlowController)
    serial_flow.acknowledge = AsyncMock()
    serial_flow.send = AsyncMock(return_value=True)
    mqtt_flow = MagicMock(spec=MqttTransport)
    mqtt_flow.publish = AsyncMock()

    return SystemComponent(
        config=config,
        state=state,
        serial_flow=serial_flow,
        mqtt_flow=mqtt_flow
    )


@pytest.mark.asyncio
async def test_system_handle_get_version_resp(system_component: SystemComponent) -> None:
    payload = msgspec.msgpack.encode(VersionResponsePacket(major=1, minor=2, patch=3))
    await system_component.handle_get_version_resp(0, payload)

    assert system_component.state.mcu_version == (1, 2, 3)
    system_component.mqtt_flow.publish.assert_called()


@pytest.mark.asyncio
async def test_system_handle_get_free_memory_resp(system_component: SystemComponent) -> None:
    payload = msgspec.msgpack.encode(FreeMemoryResponsePacket(value=2048))
    await system_component.handle_get_free_memory_resp(0, payload)

    system_component.mqtt_flow.publish.assert_called()


@pytest.mark.asyncio
async def test_system_handle_mqtt_bootloader(system_component: SystemComponent) -> None:
    route = make_route(Topic.SYSTEM, SystemAction.BOOTLOADER.value)
    msg = make_mqtt_msg(b"")

    await system_component.handle_mqtt(route, msg)

    system_component.serial_flow.send.assert_called()
    assert system_component.serial_flow.send.call_args[0][0] == Command.CMD_ENTER_BOOTLOADER.value


@pytest.mark.asyncio
async def test_system_request_mcu_version(system_component: SystemComponent) -> None:
    await system_component.request_mcu_version()

    system_component.serial_flow.send.assert_called_with(Command.CMD_GET_VERSION.value, b"")
    assert system_component.state.mcu_version is None
