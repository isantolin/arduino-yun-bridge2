import msgspec
import pytest
from typing import Any, cast
from unittest.mock import AsyncMock
from mcubridge.services.mailbox import MailboxComponent
from mcubridge.protocol.structures import TopicRoute, MailboxPushPacket
from mcubridge.protocol.topics import Topic
from mcubridge.protocol.protocol import MailboxAction
from aiomqtt.message import Message


from mcubridge.services.serial_flow import SerialFlowController
from mcubridge.transport.mqtt import MqttTransport


@pytest.fixture
def mailbox_component(runtime_config: Any, runtime_state: Any) -> MailboxComponent:
    # [SIL-2] Use AsyncMock(spec=Interface) for all component mocks
    serial_flow = AsyncMock(spec=SerialFlowController)
    serial_flow.send = AsyncMock()
    mqtt_flow = AsyncMock(spec=MqttTransport)
    mqtt_flow.enqueue_mqtt = AsyncMock()
    return MailboxComponent(runtime_config, runtime_state, serial_flow, mqtt_flow)


@pytest.mark.asyncio
async def test_handle_push_stores_in_incoming_queue(
    mailbox_component: MailboxComponent, runtime_state: Any
):
    # Use proper encoding from the protocol structures
    payload = msgspec.msgpack.encode(MailboxPushPacket(data=b"some-data"))
    await mailbox_component.handle_push(1, payload)
    assert len(runtime_state.mailbox_incoming_queue) == 1
    popped = runtime_state.mailbox_incoming_queue.popleft()
    assert popped == b"some-data"


@pytest.mark.asyncio
async def test_handle_available_replies_if_not_empty(
    mailbox_component: MailboxComponent, runtime_state: Any
):
    runtime_state.mailbox_queue.append(b"msg1")
    await mailbox_component.handle_available(1, b"")
    assert cast(Any, mailbox_component.serial_flow.send).called


@pytest.mark.asyncio
async def test_handle_read_sends_pop(
    mailbox_component: MailboxComponent, runtime_state: Any
):
    runtime_state.mailbox_queue.append(b"msg1")
    await mailbox_component.handle_read(1, b"")
    assert cast(Any, mailbox_component.serial_flow.send).called


@pytest.mark.asyncio
async def test_handle_mqtt_logic(
    mailbox_component: MailboxComponent, runtime_state: Any
):
    # Test write via MQTT (Must use MailboxAction.WRITE.value)
    route = TopicRoute(
        raw="br/mailbox/write",
        prefix="br",
        topic=Topic.MAILBOX,
        segments=(MailboxAction.WRITE.value,),
    )
    msg = Message(Topic.MAILBOX.value, b"mcu-data", 0, False, False, None)
    await mailbox_component.handle_mqtt(route, msg)
    assert len(runtime_state.mailbox_queue) == 1

    # Test read via MQTT
    route_read = TopicRoute(
        raw="br/mailbox/read",
        prefix="br",
        topic=Topic.MAILBOX,
        segments=(MailboxAction.READ.value,),
    )
    msg_read = Message(Topic.MAILBOX.value, b"", 0, False, False, None)
    runtime_state.mailbox_incoming_queue.append(b"mcu-reply")
    await mailbox_component.handle_mqtt(route_read, msg_read)
    assert cast(Any, mailbox_component.mqtt_flow.enqueue_mqtt).called
