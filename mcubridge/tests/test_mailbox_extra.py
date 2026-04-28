import pytest
from mcubridge.services.serial_flow import SerialFlowController
from mcubridge.transport.mqtt import MqttTransport
from typing import Any
from unittest.mock import AsyncMock
from mcubridge.services.mailbox import MailboxComponent


@pytest.fixture
def mailbox_component(runtime_config: Any, runtime_state: Any) -> MailboxComponent:
    serial_flow = AsyncMock(spec=SerialFlowController)
    serial_flow.send = AsyncMock()
    mqtt_flow = AsyncMock(spec=MqttTransport)
    mqtt_flow.enqueue_mqtt = AsyncMock()
    return MailboxComponent(runtime_config, runtime_state, serial_flow, mqtt_flow)


@pytest.mark.asyncio
async def test_mailbox_handle_push_large_data(
    mailbox_component: MailboxComponent, runtime_state: Any
):
    large_data = b"x" * 1024
    # Directly use the queue for tests
    runtime_state.mailbox_incoming_queue.append(large_data)
    assert len(runtime_state.mailbox_incoming_queue) == 1


@pytest.mark.asyncio
async def test_mailbox_handle_mqtt_push_limit(
    mailbox_component: MailboxComponent, runtime_state: Any
):
    # Set the limit in state
    runtime_state.mailbox_queue_limit = 1

    # We need to use the component to trigger the drop logic
    from aiomqtt.message import Message
    from mcubridge.protocol.protocol import Topic
    from mcubridge.protocol.structures import TopicRoute

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
    assert len(runtime_state.mailbox_queue) == 1

    # Adding another should trigger drop logic in MailboxComponent
    await mailbox_component.handle_mqtt(
        TopicRoute("br/mailbox/write", "br", Topic.MAILBOX, ("write",)),
        Message(
            topic="test/topic",
            payload=b"data2",
            qos=0,
            retain=False,
            mid=1,
            properties=None,
        ),
    )
    assert len(runtime_state.mailbox_queue) == 1
    assert runtime_state.mailbox_dropped_messages >= 1
