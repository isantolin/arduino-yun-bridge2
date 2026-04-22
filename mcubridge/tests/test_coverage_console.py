from __future__ import annotations

from typing import Any, cast
from unittest.mock import AsyncMock

import pytest
from mcubridge.services.console import ConsoleComponent
from mcubridge.state.context import create_runtime_state
from mcubridge.protocol.protocol import ConsoleAction
from mcubridge.protocol.structures import TopicRoute
from mcubridge.protocol.topics import Topic
from aiomqtt.message import Message

@pytest.fixture
def console_comp(runtime_config: Any):
    state = create_runtime_state(runtime_config)
    comp = ConsoleComponent(
        config=runtime_config,
        state=state,
        serial_flow=AsyncMock(),
        mqtt_flow=AsyncMock()
    )
    return comp

@pytest.mark.asyncio
async def test_console_handle_write_malformed(console_comp: ConsoleComponent):
    await console_comp.handle_write(0, b"bad-msgpack")
    assert not console_comp.mqtt_flow.publish.called

@pytest.mark.asyncio
async def test_console_mqtt_input_paused(console_comp: ConsoleComponent):
    console_comp.state.mcu_is_paused = True
    await console_comp._handle_mqtt_input(b"some-data")
    assert len(console_comp.state.console_to_mcu_queue) == 1

@pytest.mark.asyncio
async def test_console_mqtt_input_send_fail(console_comp: ConsoleComponent):
    cast(AsyncMock, console_comp.serial_flow.send).return_value = False
    await console_comp._handle_mqtt_input(b"some-data")
    assert len(console_comp.state.console_to_mcu_queue) == 1

@pytest.mark.asyncio
async def test_console_flush_queue_send_fail(console_comp: ConsoleComponent):
    console_comp.state.console_to_mcu_queue.append(b"data")
    cast(AsyncMock, console_comp.serial_flow.send).return_value = False
    await console_comp.flush_queue()
    assert len(console_comp.state.console_to_mcu_queue) == 1

@pytest.mark.asyncio
async def test_console_handle_mqtt(console_comp: ConsoleComponent):
    route = TopicRoute(raw="", prefix="br", topic=Topic.CONSOLE, segments=(ConsoleAction.IN.value,))
    msg = Message("br/console/in", b"data", 0, False, False, None)
    ok = await console_comp.handle_mqtt(route, msg)
    assert ok is True
