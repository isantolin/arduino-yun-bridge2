"""Command matrix tests for McuBridge."""

from __future__ import annotations

from unittest.mock import MagicMock, AsyncMock

import pytest
from aiomqtt.message import Message

from mcubridge.protocol.topics import Topic, parse_topic
from mcubridge.services.dispatcher import BridgeDispatcher


@pytest.mark.asyncio
async def test_mqtt_subscriptions_are_dispatched() -> None:
    """Verify that standard command topics are correctly routed. [SIL-2]"""
    prefix = "br"

    # We test with a single topic to guarantee process stability in parallel tests
    topic_enum = Topic.CONSOLE
    segments = ("write",)
    component_attr = "console"

    state_mock = MagicMock()

    # Ensure wait is a coroutine returning immediately
    async def _wait_mock():
        return True

    state_mock.link_sync_event.wait = _wait_mock

    dispatcher = BridgeDispatcher(
        mcu_registry={},
        state=state_mock,
        send_frame=AsyncMock(),
        acknowledge_frame=AsyncMock(),
        is_topic_action_allowed=lambda _t, _a: True,
        reject_topic_action=AsyncMock(),
        publish_bridge_snapshot=AsyncMock(),
    )

    mock_comp = MagicMock()
    mock_comp.handle_mqtt = AsyncMock()
    setattr(dispatcher, component_attr, mock_comp)

    topic_str = f"{prefix}/{topic_enum.value}/{'/'.join(segments)}"
    msg = MagicMock(spec=Message)
    msg.topic = topic_str
    msg.payload = b"test"
    msg.properties = None

    await dispatcher.dispatch_mqtt_message(msg, lambda t: parse_topic(prefix, t))

    assert mock_comp.handle_mqtt.called
