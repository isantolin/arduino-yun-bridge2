"""Extra unit tests for MailboxComponent (SIL-2)."""

from __future__ import annotations

from typing import Any
from unittest.mock import AsyncMock

import pytest
from aiomqtt.message import Message

from mcubridge.protocol.protocol import Topic
from mcubridge.protocol.structures import TopicRoute
from mcubridge.services.mailbox import MailboxComponent


from mcubridge.services.serial_flow import SerialFlowController
from mcubridge.state.context import create_runtime_state


@pytest.fixture
def mailbox_component(
    runtime_config: Any,
) -> MailboxComponent:
    state = create_runtime_state(runtime_config)
    state.mqtt_topic_prefix = "br"

    serial_flow = AsyncMock(spec=SerialFlowController)
    serial_flow.acknowledge = AsyncMock()
    serial_flow.send = AsyncMock(return_value=True)
    enqueue_mqtt = AsyncMock()

    return MailboxComponent(runtime_config, state, serial_flow, enqueue_mqtt)


@pytest.mark.asyncio
async def test_mailbox_handle_mqtt_push_limit(
    mailbox_component: MailboxComponent, runtime_state: Any
):
    # Set the limit in state
    mailbox_component.state.mailbox_queue_limit = 1

    # Fill queue
    mailbox_component.state.mailbox_queue.append(b"data-pre")

    # We need to use the component to trigger the push logic
    await mailbox_component.handle_mqtt(
        TopicRoute("br/mailbox/write", "br", Topic.MAILBOX, ("write",)),
        Message(
            topic="test/topic",
            payload=b"data1",
            qos=0,
            retain=False,
            mid=1,
            properties=None,
        ),
    )

    assert len(mailbox_component.state.mailbox_queue) == 1
    # In current implementation, we drop the NEW data on overflow
    assert mailbox_component.state.mailbox_queue[0] == b"data-pre"


@pytest.mark.asyncio
async def test_mailbox_handle_mqtt_read_empty(
    mailbox_component: MailboxComponent, runtime_state: Any
):
    # Ensure queue is empty
    mailbox_component.state.mailbox_incoming_queue.clear()

    await mailbox_component.handle_mqtt(
        TopicRoute("br/mailbox/read", "br", Topic.MAILBOX, ("read",)),
        Message(
            topic="test/topic",
            payload=b"",
            qos=0,
            retain=False,
            mid=1,
            properties=None,
        ),
    )
    # Should have enqueued an available=0 message
    mailbox_component.enqueue_mqtt.assert_called()
    msg = mailbox_component.enqueue_mqtt.call_args.args[0]
    assert "incoming_available" in msg.topic_name
    assert msg.payload == b"0"
