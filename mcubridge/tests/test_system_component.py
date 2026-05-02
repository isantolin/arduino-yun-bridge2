"""Unit tests for SystemComponent (SIL-2)."""

from __future__ import annotations

import collections
from typing import Any, cast
from unittest.mock import AsyncMock, MagicMock

import msgspec
import pytest
from aiomqtt.message import Message

from mcubridge.config.settings import RuntimeConfig
from mcubridge.protocol import structures
from mcubridge.protocol.protocol import Command, SystemAction
from mcubridge.protocol.topics import Topic, TopicRoute
from mcubridge.services.serial_flow import SerialFlowController
from mcubridge.services.system import SystemComponent
from mcubridge.state.context import create_runtime_state


@pytest.fixture
def system_component(runtime_config: RuntimeConfig) -> SystemComponent:
    state = create_runtime_state(runtime_config)
    serial_flow = AsyncMock(spec=SerialFlowController)
    serial_flow.send = AsyncMock(return_value=True)
    enqueue_mqtt = AsyncMock()
    return SystemComponent(runtime_config, state, serial_flow, enqueue_mqtt)


@pytest.mark.asyncio
async def test_system_request_mcu_version(system_component: SystemComponent) -> None:
    await system_component.request_mcu_version()
    cast(Any, system_component.serial_flow.send).assert_called_with(
        Command.CMD_GET_VERSION.value, b""
    )


@pytest.mark.asyncio
async def test_system_handle_get_version_resp(system_component: SystemComponent) -> None:
    pkt = structures.VersionResponsePacket(major=2, minor=0, patch=1)
    payload = msgspec.msgpack.encode(pkt)

    await system_component.handle_get_version_resp(0, payload)

    assert system_component.state.mcu_version == (2, 0, 1)
    cast(Any, system_component.enqueue_mqtt).assert_called()
    msg = cast(Any, system_component.enqueue_mqtt).call_args.args[0]
    assert msg.payload == b"2.0.1"


@pytest.mark.asyncio
async def test_system_handle_get_free_memory_resp(
    system_component: SystemComponent,
) -> None:
    pkt = structures.FreeMemoryResponsePacket(value=1024)
    payload = msgspec.msgpack.encode(pkt)

    await system_component.handle_get_free_memory_resp(0, payload)

    cast(Any, system_component.enqueue_mqtt).assert_called()
    msg = cast(Any, system_component.enqueue_mqtt).call_args.args[0]
    assert msg.payload == b"1024"


@pytest.mark.asyncio
async def test_system_handle_mqtt_free_memory(system_component: SystemComponent) -> None:
    route = TopicRoute(
        raw=f"br/{Topic.SYSTEM}/{SystemAction.FREE_MEMORY.value}",
        prefix="br",
        topic=Topic.SYSTEM,
        segments=(SystemAction.FREE_MEMORY.value,),
    )
    msg = Message(
        topic="test/topic", payload=b"", qos=0, retain=False, mid=1, properties=None
    )

    await system_component.handle_mqtt(route, msg)

    cast(Any, system_component.serial_flow.send).assert_called_with(
        Command.CMD_GET_FREE_MEMORY.value, b""
    )


@pytest.mark.asyncio
async def test_system_handle_mqtt_bootloader(system_component: SystemComponent) -> None:
    route = TopicRoute(
        raw=f"br/{Topic.SYSTEM}/{SystemAction.BOOTLOADER.value}",
        prefix="br",
        topic=Topic.SYSTEM,
        segments=(SystemAction.BOOTLOADER.value,),
    )
    # [SIL-2] Provide valid payload for bootloader request
    pkt = structures.EnterBootloaderPacket(magic=0xDEADBEEF)
    msg = Message(
        topic="test/topic",
        payload=msgspec.msgpack.encode(pkt),
        qos=0,
        retain=False,
        mid=1,
        properties=None,
    )

    await system_component.handle_mqtt(route, msg)

    cast(Any, system_component.serial_flow.send).assert_called()
    assert cast(Any, system_component.serial_flow.send).call_args.args[0] == Command.CMD_ENTER_BOOTLOADER.value
