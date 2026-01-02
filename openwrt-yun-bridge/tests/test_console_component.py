"""Tests for the ConsoleComponent."""

from __future__ import annotations

from unittest.mock import AsyncMock, patch

import pytest
import pytest_asyncio

from yunbridge.config.settings import RuntimeConfig
from yunbridge.const import (
    DEFAULT_MQTT_PORT,
    DEFAULT_MQTT_TOPIC,
    DEFAULT_PROCESS_TIMEOUT,
    DEFAULT_RECONNECT_DELAY,
    DEFAULT_STATUS_INTERVAL,
)
from yunbridge.rpc import protocol
from yunbridge.rpc.protocol import Command, MAX_PAYLOAD_SIZE
from yunbridge.services.components.base import BridgeContext
from yunbridge.services.components.console import ConsoleComponent
from yunbridge.state.context import create_runtime_state


@pytest_asyncio.fixture
async def console_component() -> ConsoleComponent:
    config = RuntimeConfig(
        serial_port="/dev/null",
        serial_baud=protocol.DEFAULT_BAUDRATE,
        serial_safe_baud=protocol.DEFAULT_SAFE_BAUDRATE,
        mqtt_host="localhost",
        mqtt_port=DEFAULT_MQTT_PORT,
        mqtt_user=None,
        mqtt_pass=None,
        mqtt_tls=False,
        mqtt_cafile=None,
        mqtt_certfile=None,
        mqtt_keyfile=None,
        mqtt_topic=DEFAULT_MQTT_TOPIC,
        allowed_commands=(),
        file_system_root="/tmp",
        process_timeout=DEFAULT_PROCESS_TIMEOUT,
        reconnect_delay=DEFAULT_RECONNECT_DELAY,
        status_interval=DEFAULT_STATUS_INTERVAL,
        serial_shared_secret=b"testsecret",
    )
    state = create_runtime_state(config)
    ctx = AsyncMock(spec=BridgeContext)

    # Mock schedule_background to just await the coroutine immediately for testing
    async def _schedule(coro):
        await coro

    ctx.schedule_background.side_effect = _schedule

    component = ConsoleComponent(config, state, ctx)
    return component


@pytest.mark.asyncio
async def test_handle_write(console_component: ConsoleComponent) -> None:
    payload = b"console output"
    await console_component.handle_write(payload)

    console_component.ctx.enqueue_mqtt.assert_awaited_once()
    msg = console_component.ctx.enqueue_mqtt.call_args[0][0]
    assert msg.payload == payload
    assert "console/out" in msg.topic_name


@pytest.mark.asyncio
async def test_flow_control(console_component: ConsoleComponent) -> None:
    # Initial state
    assert console_component.state.mcu_is_paused is False
    assert console_component.state.serial_tx_allowed.is_set() is True

    # XOFF
    await console_component.handle_xoff(b"")
    assert console_component.state.mcu_is_paused is True
    assert console_component.state.serial_tx_allowed.is_set() is False

    # XON
    with patch.object(
        console_component, "flush_queue", new_callable=AsyncMock
    ) as mock_flush:
        await console_component.handle_xon(b"")
        assert console_component.state.mcu_is_paused is False
        assert console_component.state.serial_tx_allowed.is_set() is True
        mock_flush.assert_awaited_once()


@pytest.mark.asyncio
async def test_handle_mqtt_input_direct(console_component: ConsoleComponent) -> None:
    payload = b"input"
    console_component.ctx.send_frame.return_value = True

    await console_component.handle_mqtt_input(payload)

    console_component.ctx.send_frame.assert_awaited_once_with(
        Command.CMD_CONSOLE_WRITE.value, payload
    )


@pytest.mark.asyncio
async def test_handle_mqtt_input_paused(console_component: ConsoleComponent) -> None:
    console_component.state.mcu_is_paused = True
    payload = b"input"

    await console_component.handle_mqtt_input(payload)

    console_component.ctx.send_frame.assert_not_awaited()
    assert len(console_component.state.console_to_mcu_queue) == 1
    assert console_component.state.console_to_mcu_queue[0] == payload


@pytest.mark.asyncio
async def test_handle_mqtt_input_chunking(console_component: ConsoleComponent) -> None:
    # Payload larger than MAX_PAYLOAD_SIZE
    # We need to account for overhead if any, but console chunks are raw?
    # Let's check _iter_console_chunks implementation or assume it chunks by MAX_PAYLOAD_SIZE - overhead
    # Assuming overhead is small or zero for console write command payload itself?
    # protocol.MAX_PAYLOAD_SIZE comes from tools/protocol/spec.toml.

    large_payload = b"a" * (MAX_PAYLOAD_SIZE + 10)
    console_component.ctx.send_frame.return_value = True

    await console_component.handle_mqtt_input(large_payload)

    assert console_component.ctx.send_frame.await_count >= 2


@pytest.mark.asyncio
async def test_flush_queue(console_component: ConsoleComponent) -> None:
    console_component.state.enqueue_console_chunk(b"queued", None)
    console_component.ctx.send_frame.return_value = True

    await console_component.flush_queue()

    console_component.ctx.send_frame.assert_awaited_once_with(
        Command.CMD_CONSOLE_WRITE.value, b"queued"
    )
    assert len(console_component.state.console_to_mcu_queue) == 0
