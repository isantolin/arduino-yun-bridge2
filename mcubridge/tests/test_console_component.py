"""Unit tests for mcubridge.services.console."""

from __future__ import annotations

import asyncio
from collections import deque
from typing import cast
from unittest.mock import AsyncMock, MagicMock

import pytest
from mcubridge.config.settings import RuntimeConfig
from mcubridge.protocol import protocol
from mcubridge.protocol.structures import ConsoleWritePacket
from mcubridge.services.base import BridgeContext
from mcubridge.services.console import ConsoleComponent
from mcubridge.state.context import RuntimeState

MAX_PAYLOAD_SIZE = protocol.MAX_PAYLOAD_SIZE


@pytest.fixture()
def console_component() -> ConsoleComponent:
    config = MagicMock(spec=RuntimeConfig)
    state = MagicMock(spec=RuntimeState)
    state.mqtt_topic_prefix = "bridge"
    state.mcu_is_paused = False
    state.serial_tx_allowed = asyncio.Event()
    state.serial_tx_allowed.set()
    state.console_to_mcu_queue = deque()
    state.pop_console_chunk = MagicMock()

    state.enqueue_console_chunk = MagicMock()
    state.requeue_console_chunk_front = MagicMock()

    ctx = MagicMock(spec=BridgeContext)
    ctx.state = state
    ctx.config = config
    ctx.serial_flow = MagicMock()
    ctx.serial_flow.send = AsyncMock(return_value=True)
    ctx.mqtt_flow = MagicMock()
    ctx.mqtt_flow.publish = AsyncMock()
    ctx.mqtt_flow.enqueue_mqtt = AsyncMock()

    return ConsoleComponent(config, state, ctx)


@pytest.mark.asyncio
async def test_handle_write(console_component: ConsoleComponent) -> None:
    data = b"hello world"
    payload = ConsoleWritePacket(data=data).encode()

    await console_component.handle_write(0, payload)

    mock_pub = cast(AsyncMock, console_component.ctx.mqtt_flow.publish)
    mock_pub.assert_called_once()
    args, kwargs = mock_pub.call_args
    # Check if data was published
    published_payload = kwargs.get("payload") or (args[1] if len(args) > 1 else None)
    assert published_payload == data


@pytest.mark.asyncio
async def test_flow_control(console_component: ConsoleComponent) -> None:
    # Test XOFF
    await console_component.handle_xoff(0, b"")
    assert console_component.state.mcu_is_paused is True
    assert not console_component.state.serial_tx_allowed.is_set()

    # Test XON
    await console_component.handle_xon(0, b"")
    assert console_component.state.mcu_is_paused is False
    assert console_component.state.serial_tx_allowed.is_set()


@pytest.mark.asyncio
async def test_handle_mqtt_input_direct(console_component: ConsoleComponent) -> None:
    payload = b"input"
    await console_component._handle_mqtt_input(payload)  # type: ignore[reportPrivateUsage]

    console_component.ctx.serial_flow.send.assert_awaited_once()


@pytest.mark.asyncio
async def test_handle_mqtt_input_paused(console_component: ConsoleComponent) -> None:
    console_component.state.mcu_is_paused = True
    payload = b"input"

    await console_component._handle_mqtt_input(payload)  # type: ignore[reportPrivateUsage]

    console_component.ctx.serial_flow.send.assert_not_called()
    mock_enqueue = cast(MagicMock, console_component.state.enqueue_console_chunk)
    assert len(mock_enqueue.call_args_list) == 1


@pytest.mark.asyncio
async def test_handle_mqtt_input_chunking(console_component: ConsoleComponent) -> None:
    # Payload larger than MAX_PAYLOAD_SIZE
    large_payload = b"a" * (MAX_PAYLOAD_SIZE + 10)

    await console_component._handle_mqtt_input(large_payload)  # type: ignore[reportPrivateUsage]

    assert console_component.ctx.serial_flow.send.call_count >= 2


@pytest.mark.asyncio
async def test_flush_queue(console_component: ConsoleComponent) -> None:
    # Setup mock state behavior
    queue = deque([b"queued"])
    console_component.state.console_to_mcu_queue = queue  # type: ignore[reportAttributeAccessIssue]
    mock_pop = cast(MagicMock, console_component.state.pop_console_chunk)
    mock_pop.side_effect = lambda: queue.popleft() if queue else None

    await console_component.flush_queue()

    console_component.ctx.serial_flow.send.assert_awaited_once()
