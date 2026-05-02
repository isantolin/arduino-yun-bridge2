"""Unit tests for ConsoleComponent MCU/MQTT behaviour (SIL-2)."""

from __future__ import annotations

from unittest.mock import AsyncMock

import msgspec
import pytest

from mcubridge.config.settings import RuntimeConfig
from mcubridge.protocol import structures
from mcubridge.services.console import ConsoleComponent
from mcubridge.services.serial_flow import SerialFlowController
from mcubridge.state.context import create_runtime_state


@pytest.fixture
def console_component(
    runtime_config: RuntimeConfig,
) -> ConsoleComponent:
    state = create_runtime_state(runtime_config)
    state.mqtt_topic_prefix = "br"

    serial_flow = AsyncMock(spec=SerialFlowController)
    serial_flow.acknowledge = AsyncMock()
    serial_flow.send = AsyncMock(return_value=True)
    enqueue_mqtt = AsyncMock()

    return ConsoleComponent(runtime_config, state, serial_flow, enqueue_mqtt)


@pytest.mark.asyncio
async def test_console_handle_write_success(
    console_component: ConsoleComponent,
) -> None:
    # MCU writes console output - MUST be MsgPack encoded packet
    pkt = structures.ConsoleWritePacket(data=b"hello world")
    payload = msgspec.msgpack.encode(pkt)

    await console_component.handle_write(0, payload)

    console_component.enqueue_mqtt.assert_called()
    msg = console_component.enqueue_mqtt.call_args.args[0]
    assert msg.payload == b"hello world"
    assert "console/out" in msg.topic_name


@pytest.mark.asyncio
async def test_console_xoff_xon(console_component: ConsoleComponent) -> None:
    await console_component.handle_xoff(0, b"")
    assert console_component.state.mcu_is_paused is True

    await console_component.handle_xon(0, b"")
    assert console_component.state.mcu_is_paused is False


@pytest.mark.asyncio
async def test_console_on_serial_disconnected(
    console_component: ConsoleComponent,
) -> None:
    console_component.state.mcu_is_paused = True
    await console_component.on_serial_disconnected()
    assert console_component.state.mcu_is_paused is False
    assert len(console_component.state.console_to_mcu_queue) == 0
