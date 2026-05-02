"""Extra coverage for ConsoleComponent (SIL-2)."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest
from aiomqtt.message import Message

from mcubridge.config.settings import RuntimeConfig
from mcubridge.protocol.protocol import Command, Topic
from mcubridge.protocol.structures import TopicRoute
from mcubridge.services.console import ConsoleComponent
from mcubridge.services.serial_flow import SerialFlowController
from mcubridge.state.context import create_runtime_state


@pytest.fixture
def console_comp(runtime_config: RuntimeConfig) -> ConsoleComponent:
    state = create_runtime_state(runtime_config)
    serial_flow = AsyncMock(spec=SerialFlowController)
    serial_flow.send = AsyncMock(return_value=True)
    enqueue_mqtt = AsyncMock()
    return ConsoleComponent(runtime_config, state, serial_flow, enqueue_mqtt)


@pytest.mark.asyncio
async def test_console_mqtt_input_paused(console_comp: ConsoleComponent) -> None:
    # Pause MCU
    console_comp.state.mcu_is_paused = True

    msg = MagicMock(spec=Message)
    msg.payload = b"hello"
    route = TopicRoute("br/console/in", "br", Topic.CONSOLE, ("in",))

    await console_comp.handle_mqtt(route, msg)

    # Should be in queue, not sent
    assert len(console_comp.state.console_to_mcu_queue) == 1
    assert isinstance(console_comp.serial_flow.send, AsyncMock)
    console_comp.serial_flow.send.assert_not_called()


@pytest.mark.asyncio
async def test_console_handle_mqtt(console_comp: ConsoleComponent) -> None:
    # Normal input
    msg = MagicMock(spec=Message)
    msg.payload = b"hello"
    route = TopicRoute("br/console/in", "br", Topic.CONSOLE, ("in",))

    await console_comp.handle_mqtt(route, msg)

    assert isinstance(console_comp.serial_flow.send, AsyncMock)
    console_comp.serial_flow.send.assert_called()
    assert (
        console_comp.serial_flow.send.call_args.args[0]
        == Command.CMD_CONSOLE_WRITE.value
    )
    assert b"hello" in console_comp.serial_flow.send.call_args.args[1]
