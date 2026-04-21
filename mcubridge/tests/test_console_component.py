"""Unit tests for the ConsoleComponent."""

from __future__ import annotations

import collections
from unittest.mock import AsyncMock, MagicMock

import msgspec
import pytest
from mcubridge.protocol.structures import ConsoleWritePacket
from mcubridge.services.console import ConsoleComponent
from mcubridge.services.serial_flow import SerialFlowController
from mcubridge.state.context import RuntimeState
from mcubridge.transport.mqtt import MqttTransport
from tests._helpers import make_test_config


@pytest.fixture
def console_component() -> ConsoleComponent:
    config = make_test_config()
    state = MagicMock(spec=RuntimeState)
    state.mqtt_topic_prefix = "br"
    state.mcu_is_paused = False
    state.console_to_mcu_queue = collections.deque()

    serial_flow = MagicMock(spec=SerialFlowController)
    serial_flow.send = AsyncMock(return_value=True)
    mqtt_flow = MagicMock(spec=MqttTransport)
    mqtt_flow.publish = AsyncMock()

    return ConsoleComponent(
        config=config,
        state=state,
        serial_flow=serial_flow,
        mqtt_flow=mqtt_flow
    )


@pytest.mark.asyncio
async def test_console_handle_write_success(console_component: ConsoleComponent) -> None:
    # [SIL-2] Use direct msgspec.msgpack.encode (Zero Wrapper)
    payload = msgspec.msgpack.encode(ConsoleWritePacket(data=b"hello"))

    await console_component.handle_write(0, payload)

    console_component.mqtt_flow.publish.assert_called()


@pytest.mark.asyncio
async def test_console_xoff_xon(console_component: ConsoleComponent) -> None:
    await console_component.handle_xoff(0, b"")
    assert console_component.state.mcu_is_paused is True

    await console_component.handle_xon(1, b"")
    assert console_component.state.mcu_is_paused is False


@pytest.mark.asyncio
async def test_console_on_serial_disconnected(console_component: ConsoleComponent) -> None:
    console_component.state.mcu_is_paused = True
    console_component.on_serial_disconnected()
    assert console_component.state.mcu_is_paused is False
