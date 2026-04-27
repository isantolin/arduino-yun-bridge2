"""Unit tests for the SystemComponent."""

from __future__ import annotations

from typing import Any, cast
from unittest.mock import AsyncMock

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
from tests._helpers import make_route, make_test_config
from tests.mqtt_helpers import make_inbound_message


@pytest.fixture
def system_component() -> SystemComponent:
    config = make_test_config()
    # [SIL-2] Use AsyncMock(spec=Interface) for all component mocks
    state = AsyncMock(spec=RuntimeState)
    state.mqtt_topic_prefix = "br"
    state.mcu_version = None

    serial_flow = AsyncMock(spec=SerialFlowController)
    serial_flow.acknowledge = AsyncMock()
    serial_flow.send = AsyncMock(return_value=True)
    mqtt_flow = AsyncMock(spec=MqttTransport)
    mqtt_flow.publish = AsyncMock()

    return SystemComponent(
        config=config, state=state, serial_flow=serial_flow, mqtt_flow=mqtt_flow
    )


@pytest.mark.asyncio
async def test_system_handle_get_version_resp(
    system_component: SystemComponent,
) -> None:
    payload = msgspec.msgpack.encode(VersionResponsePacket(major=1, minor=2, patch=3))
    await system_component.handle_get_version_resp(0, payload)

    assert system_component.state.mcu_version == (1, 2, 3)
    cast(Any, system_component.mqtt_flow.publish).assert_called()


@pytest.mark.asyncio
async def test_system_handle_get_free_memory_resp(
    system_component: SystemComponent,
) -> None:
    payload = msgspec.msgpack.encode(FreeMemoryResponsePacket(value=2048))
    await system_component.handle_get_free_memory_resp(0, payload)

    cast(Any, system_component.mqtt_flow.publish).assert_called()


@pytest.mark.asyncio
async def test_system_handle_mqtt_bootloader(system_component: SystemComponent) -> None:
    route = make_route(Topic.SYSTEM, SystemAction.BOOTLOADER.value)
    msg = make_inbound_message("test/topic", b"")

    await system_component.handle_mqtt(route, msg)

    cast(Any, system_component.serial_flow.send).assert_called()
    assert (
        cast(Any, system_component.serial_flow.send).call_args[0][0]
        == Command.CMD_ENTER_BOOTLOADER.value
    )


@pytest.mark.asyncio
async def test_system_request_mcu_version(system_component: SystemComponent) -> None:
    await system_component.request_mcu_version()

    cast(Any, system_component.serial_flow.send).assert_called_with(
        Command.CMD_GET_VERSION.value, b""
    )
    assert system_component.state.mcu_version is None
