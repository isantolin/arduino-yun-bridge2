"""Extra edge-case tests for ConsoleComponent (SIL-2)."""

from __future__ import annotations

import os
import time
from unittest.mock import AsyncMock, MagicMock

import pytest
from mcubridge.config.settings import RuntimeConfig
from mcubridge.protocol.protocol import Command
from mcubridge.protocol.topics import Topic, TopicRoute
from mcubridge.services.console import ConsoleComponent
from mcubridge.state.context import create_runtime_state
from tests._helpers import make_mqtt_msg


@pytest.mark.asyncio
async def test_console_handle_write_malformed() -> None:
    config = RuntimeConfig(serial_shared_secret=b"secret_1234")
    state = create_runtime_state(config)
    try:
        ctx = MagicMock()
        ctx.mqtt_flow = MagicMock()
        ctx.mqtt_flow.publish = AsyncMock()

        comp = ConsoleComponent(config, state, ctx)

        # Malformed ConsoleWritePacket (too short)
        await comp.handle_write(0, b"")

        ctx.mqtt_flow.publish.assert_not_called()
    finally:
        state.cleanup()


@pytest.mark.asyncio
async def test_console_mqtt_input_error_paths() -> None:
    config = RuntimeConfig(serial_shared_secret=b"secret_1234")
    state = create_runtime_state(config)
    try:
        ctx = MagicMock()
        ctx.serial_flow = MagicMock()
        # Simulate serial failure
        ctx.serial_flow.send = AsyncMock(return_value=False)

        def _chunk(p, size):
            return [p[i:i+size] for i in range(0, len(p), size)] if p else []
        ctx.serial_flow.chunk_payload.side_effect = _chunk

        comp = ConsoleComponent(config, state, ctx)

        # Sending input when serial fails should queue it
        route = TopicRoute("br/console/in", "br", Topic.CONSOLE, ("in",))
        inbound = make_mqtt_msg(b"lost-data")
        await comp.handle_mqtt_in(route, inbound)

        assert len(state.console_to_mcu_queue) == 1
    finally:
        state.cleanup()


@pytest.mark.asyncio
async def test_console_flush_queue_serial_fail() -> None:
    config = RuntimeConfig(serial_shared_secret=b"secret_1234")
    state = create_runtime_state(config)
    try:
        ctx = MagicMock()
        ctx.serial_flow = MagicMock()
        # Initial success then failure
        ctx.serial_flow.send = AsyncMock(side_effect=[True, False])

        def _chunk(p, size):
            return [p[i:i+size] for i in range(0, len(p), size)] if p else []
        ctx.serial_flow.chunk_payload.side_effect = _chunk

        comp = ConsoleComponent(config, state, ctx)

        # Add multiple chunks to queue
        state.enqueue_console_chunk(b"chunk1")
        state.enqueue_console_chunk(b"chunk2")

        await comp.flush_queue()

        # chunk2 should be requeued
        assert len(state.console_to_mcu_queue) == 1
    finally:
        state.cleanup()
