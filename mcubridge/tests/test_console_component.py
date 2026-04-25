"""Tests for the ConsoleComponent."""

from __future__ import annotations

import msgspec
import pytest
from unittest.mock import AsyncMock, MagicMock, patch
from mcubridge.services.console import ConsoleComponent, ConsoleAction
from mcubridge.protocol.structures import ConsoleWritePacket
from mcubridge.protocol.topics import Topic, topic_path


@pytest.fixture
def console_comp(runtime_config, runtime_state):
    serial_flow = MagicMock()
    serial_flow.send = AsyncMock()
    # Signature: config, state, serial_flow, mqtt_flow=None
    return ConsoleComponent(runtime_config, runtime_state, serial_flow)


@pytest.mark.asyncio
async def test_console_handle_write_success(console_comp):
    payload = msgspec.msgpack.encode(ConsoleWritePacket(data=b"hello"))
    # Patch the utility where it is USED (in mcubridge.services.console)
    with patch(
        "mcubridge.services.console.atomic_publish", new_callable=AsyncMock
    ) as mock_publish:
        await console_comp.handle_write(0, payload)

        expected_topic = topic_path(
            console_comp.state.mqtt_topic_prefix,
            Topic.CONSOLE,
            ConsoleAction.OUT,
        )
        mock_publish.assert_called_once()
        args, kwargs = mock_publish.call_args
        assert kwargs["topic"] == expected_topic
        assert kwargs["payload"] == b"hello"


@pytest.mark.asyncio
async def test_console_xoff_xon(console_comp):
    # This logic remains state-based, no changes needed for MQTT removal
    await console_comp.handle_xoff(0, b"")
    assert console_comp.state.mcu_is_paused is True

    await console_comp.handle_xon(0, b"")
    assert console_comp.state.mcu_is_paused is False
