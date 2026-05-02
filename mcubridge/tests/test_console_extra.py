"""Extra coverage for ConsoleComponent (SIL-2)."""

from __future__ import annotations

import collections
from typing import Any, cast
from unittest.mock import AsyncMock, MagicMock, patch

import msgspec
import pytest
from aiomqtt.message import Message

from mcubridge.config.settings import RuntimeConfig
from mcubridge.protocol import structures
from mcubridge.protocol.protocol import Command, Status, Topic
from mcubridge.protocol.structures import TopicRoute
from mcubridge.services.console import ConsoleComponent
from mcubridge.services.serial_flow import SerialFlowController
from mcubridge.state.context import create_runtime_state


@pytest.fixture
def console_comp(runtime_config: RuntimeConfig) -> ConsoleComponent:
    state = create_runtime_state(runtime_config)
    state.mqtt_topic_prefix = "br"
    serial_flow = AsyncMock(spec=SerialFlowController)
    serial_flow.send = AsyncMock(return_value=True)
    enqueue_mqtt = AsyncMock()
    return ConsoleComponent(runtime_config, state, serial_flow, enqueue_mqtt)


@pytest.mark.asyncio
async def test_console_handle_write_edge_cases(console_comp: ConsoleComponent) -> None:
    # 1. Null payload (valid for heartbeat/EOF if wrapped in Packet)
    pkt_empty = structures.ConsoleWritePacket(data=b"")
    await console_comp.handle_write(0, msgspec.msgpack.encode(pkt_empty))
    console_comp.enqueue_mqtt.assert_called()
    assert console_comp.enqueue_mqtt.call_args.args[0].payload == b""

    # 2. Large payload (verified in component logic)
    large = b"A" * 1024
    pkt_large = structures.ConsoleWritePacket(data=large)
    await console_comp.handle_write(0, msgspec.msgpack.encode(pkt_large))
    assert console_comp.enqueue_mqtt.call_args.args[0].payload == large


@pytest.mark.asyncio
async def test_console_flush_queue_serial_failure(console_comp: ConsoleComponent) -> None:
    # Fill queue
    console_comp.state.console_to_mcu_queue.append(b"lost")
    
    # Mock send failure
    console_comp.serial_flow.send.return_value = False
    
    await console_comp.flush_queue()
    
    # In SIL-2 we requeue once then abort if serial is saturated.
    assert len(console_comp.state.console_to_mcu_queue) == 1
    assert console_comp.state.console_to_mcu_queue[0] == b"lost"
