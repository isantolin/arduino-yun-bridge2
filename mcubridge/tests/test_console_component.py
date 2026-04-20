import pytest
from unittest.mock import MagicMock, AsyncMock
from mcubridge.services.console import ConsoleComponent
from mcubridge.protocol.structures import TopicRoute, ConsoleWritePacket
from mcubridge.protocol.topics import Topic
from mcubridge.services.base import BridgeContext
from typing import Any


@pytest.fixture
def console_component(runtime_config: Any, runtime_state: Any) -> ConsoleComponent:
    # Use strict spec-based mocks as mandated
    ctx = MagicMock(spec=BridgeContext)
    ctx.serial_flow = AsyncMock()  # SerialFlow interface is dynamic but we could spec it too
    ctx.mqtt_flow = AsyncMock()
    return ConsoleComponent(runtime_config, runtime_state, ctx)


@pytest.mark.asyncio
async def test_handle_write_publishes_to_mqtt(console_component: ConsoleComponent):
    # Use proper encoding from the protocol structures
    payload = ConsoleWritePacket(data=b"hello").encode()
    await console_component.handle_write(1, payload)

    # [SIL-2] Direct verification of library calls
    console_component.ctx.mqtt_flow.publish.assert_called()
    _, kwargs = console_component.ctx.mqtt_flow.publish.call_args
    assert "console/out" in kwargs["topic"]
    assert kwargs["payload"] == b"hello"


@pytest.mark.asyncio
async def test_handle_mqtt_input_chunks_and_sends(console_component: ConsoleComponent):
    from aiomqtt.message import Message

    route = TopicRoute(raw="br/console/in", prefix="br", topic=Topic.CONSOLE, segments=("in",))
    msg = Message(Topic.CONSOLE.value, b"very long payload", 0, False, False, None)

    await console_component.handle_mqtt(route, msg)
    assert console_component.ctx.serial_flow.send.called


@pytest.mark.asyncio
async def test_handle_mqtt_input_queues_when_paused(console_component: ConsoleComponent):
    from aiomqtt.message import Message

    console_component.state.mcu_is_paused = True
    route = TopicRoute(raw="br/console/in", prefix="br", topic=Topic.CONSOLE, segments=("in",))
    msg = Message(Topic.CONSOLE.value, b"hello", 0, False, False, None)

    await console_component.handle_mqtt(route, msg)
    assert len(console_component.state.console_to_mcu_queue) > 0
    assert console_component.state.console_to_mcu_queue.popleft() == b"hello"


@pytest.mark.asyncio
async def test_flush_queue_sends_buffered_data(console_component: ConsoleComponent):
    console_component.state.console_to_mcu_queue.append(b"buffered")
    console_component.state.mcu_is_paused = False

    await console_component.flush_queue()
    assert console_component.ctx.serial_flow.send.called
    assert len(console_component.state.console_to_mcu_queue) == 0


def test_on_serial_disconnected_resets_pause(console_component: ConsoleComponent):
    console_component.state.mcu_is_paused = True
    console_component.on_serial_disconnected()
    assert console_component.state.mcu_is_paused is False
