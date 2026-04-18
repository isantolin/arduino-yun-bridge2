"""Unit tests for MailboxComponent MCU/MQTT interactions (SIL-2)."""

from __future__ import annotations

from typing import Any
from unittest.mock import AsyncMock

import pytest

from mcubridge.config.settings import RuntimeConfig
from mcubridge.protocol import structures
from mcubridge.protocol.protocol import Command, MailboxAction
from mcubridge.protocol.topics import Topic
from mcubridge.services.base import BridgeContext
from mcubridge.services.mailbox import MailboxComponent
from mcubridge.state.context import RuntimeState
from tests._helpers import make_mqtt_msg, make_route


def _extract_enqueued_publish(ctx: AsyncMock, index: int = -1) -> tuple[structures.QueuedPublish, Any]:
    """Helper to extract QueuedPublish and reply_context from AsyncMock.publish calls."""
    if not ctx.publish.called:
        raise AssertionError("publish not called")
    call = ctx.publish.call_args_list[index]
    topic = call.kwargs.get("topic", call.args[0] if call.args else "")
    payload = call.kwargs.get("payload", call.args[1] if len(call.args) > 1 else b"")

    if isinstance(payload, str):
        payload = payload.encode("utf-8")

    msg = structures.QueuedPublish(
        topic_name=topic,
        payload=payload,
        qos=call.kwargs.get("qos", 0),
        retain=call.kwargs.get("retain", False),
        content_type=call.kwargs.get("content_type"),
        message_expiry_interval=call.kwargs.get("expiry"),
        user_properties=call.kwargs.get("properties", ()),
    )
    return msg, call.kwargs.get("reply_to")


@pytest.fixture()
def mailbox_component(
    runtime_config: RuntimeConfig,
    runtime_state: RuntimeState,
) -> tuple[MailboxComponent, AsyncMock]:
    bridge = AsyncMock(spec=BridgeContext)
    bridge.config = runtime_config
    bridge.state = runtime_state
    bridge.send_frame.return_value = True

    component = MailboxComponent(runtime_config, runtime_state, bridge)
    return component, bridge


@pytest.mark.asyncio
async def test_handle_push_stores_incoming_queue(
    mailbox_component: tuple[MailboxComponent, AsyncMock],
    runtime_state: RuntimeState,
) -> None:
    component, bridge = mailbox_component

    # 1. MCU pushes a message (must be valid msgpack for MailboxPushPacket)
    payload = structures.MailboxPushPacket(data=b"hello").encode()
    await component.handle_push(0, payload)
    assert len(runtime_state.mailbox_incoming_queue) > 0

    # 2. MQTT client reads the message
    await component.handle_mqtt(
        make_route(Topic.MAILBOX, MailboxAction.READ.value),
        make_mqtt_msg(b""),
    )

    # Find the publish call that has 'hello'
    msgs = []
    for i in range(len(bridge.publish.call_args_list)):
        msg, _ = _extract_enqueued_publish(bridge, i)
        msgs.append(msg.payload)

    assert b"hello" in msgs


@pytest.mark.asyncio
async def test_handle_read_success_publishes_available(
    mailbox_component: tuple[MailboxComponent, AsyncMock],
    runtime_state: RuntimeState,
) -> None:
    component, bridge = mailbox_component
    runtime_state.enqueue_mailbox_message(b"beta")

    # MCU requests a read
    await component.handle_read(0, b"")

    bridge.send_frame.assert_called_once()
    args = bridge.send_frame.call_args.args
    command_id = args[0]
    payload = bridge.send_frame.call_args.kwargs.get("payload", args[1] if len(args) > 1 else b"")

    assert command_id == Command.CMD_MAILBOX_READ_RESP.value
    assert structures.MailboxReadResponsePacket.decode(payload).content == b"beta"

    assert bridge.publish.called


@pytest.mark.asyncio
async def test_handle_push_overflow_sends_error(
    mailbox_component: tuple[MailboxComponent, AsyncMock],
    runtime_state: RuntimeState,
) -> None:
    component, bridge = mailbox_component
    # Set limit to 1
    runtime_state.mailbox_queue_limit = 1

    # First push - should succeed
    payload1 = structures.MailboxPushPacket(data=b"msg1").encode()
    await component.handle_push(0, payload1)

    # Reset mock to catch only the second call
    bridge.send_frame.reset_mock()

    # Second push - should trigger overflow. MailboxComponent uses Status.ERROR (49)
    payload2 = structures.MailboxPushPacket(data=b"msg2").encode()
    await component.handle_push(0, payload2)

    assert bridge.send_frame.called
    assert bridge.send_frame.call_args.args[0] == 49


@pytest.mark.asyncio
async def test_handle_mqtt_write_enqueues_and_notifies(
    mailbox_component: tuple[MailboxComponent, AsyncMock],
    runtime_state: RuntimeState,
) -> None:
    component, bridge = mailbox_component

    await component.handle_mqtt(
        make_route(Topic.MAILBOX, MailboxAction.WRITE.value),
        make_mqtt_msg(b"alpha"),
    )

    assert len(runtime_state.mailbox_queue) == 1
    # WRITE only enqueues and publishes to 'outgoing_available'
    assert any("outgoing_available" in str(call.kwargs.get("topic", call.args[0] if call.args else ""))
               for call in bridge.publish.call_args_list)


@pytest.mark.asyncio
async def test_handle_mqtt_write_overflow_signals_error(
    mailbox_component: tuple[MailboxComponent, AsyncMock],
    runtime_state: RuntimeState,
) -> None:
    component, bridge = mailbox_component
    runtime_state.mailbox_queue_limit = 1
    # Directly manipulate queue for reliable overflow
    runtime_state.mailbox_queue.clear()
    runtime_state.mailbox_queue.append(b"existing")

    await component.handle_mqtt(
        make_route(Topic.MAILBOX, MailboxAction.WRITE.value),
        make_mqtt_msg(b"too-much"),
    )

    # In SIL-2 refactor, we record drops in mqtt_drop_counts or check if send_frame(ERROR) was called
    assert bridge.send_frame.called
    assert bridge.send_frame.call_args.args[0] == 49


@pytest.mark.asyncio
async def test_handle_mqtt_read_prefers_incoming_queue(
    mailbox_component: tuple[MailboxComponent, AsyncMock],
    runtime_state: RuntimeState,
) -> None:
    component, bridge = mailbox_component
    # Message already in incoming queue
    payload = structures.MailboxPushPacket(data=b"alpha").encode()
    await component.handle_push(0, payload)

    # Message in mailbox queue (lower priority)
    runtime_state.enqueue_mailbox_message(b"beta")

    await component.handle_mqtt(
        make_route(Topic.MAILBOX, MailboxAction.READ.value),
        make_mqtt_msg(b""),
    )

    # Find the publish call that has 'alpha'
    found_alpha = False
    for i in range(len(bridge.publish.call_args_list)):
        msg, _ = _extract_enqueued_publish(bridge, i)
        if msg.payload == b"alpha":
            found_alpha = True
            break
    assert found_alpha


@pytest.mark.asyncio
async def test_handle_mqtt_read_drains_mailbox_queue(
    mailbox_component: tuple[MailboxComponent, AsyncMock],
    runtime_state: RuntimeState,
) -> None:
    component, bridge = mailbox_component
    runtime_state.enqueue_mailbox_message(b"beta")

    await component.handle_mqtt(
        make_route(Topic.MAILBOX, MailboxAction.READ.value),
        make_mqtt_msg(b""),
    )

    found_beta = False
    for i in range(len(bridge.publish.call_args_list)):
        msg, _ = _extract_enqueued_publish(bridge, i)
        if msg.payload == b"beta":
            found_beta = True
            break
    assert found_beta


@pytest.mark.asyncio
async def test_handle_mqtt_read_incoming_still_notifies_on_failure(
    mailbox_component: tuple[MailboxComponent, AsyncMock],
    runtime_state: RuntimeState,
) -> None:
    component, bridge = mailbox_component
    payload = structures.MailboxPushPacket(data=b"test").encode()
    await component.handle_push(0, payload)

    async def flaky_publish(topic: str, *args: Any, **kwargs: Any) -> None:
        if str(topic).endswith("/incoming"):
            raise RuntimeError("boom")

    bridge.publish.side_effect = flaky_publish

    with pytest.raises(RuntimeError, match="boom"):
        await component.handle_mqtt(
            make_route(Topic.MAILBOX, MailboxAction.READ.value),
            make_mqtt_msg(b""),
        )

    # Recorded calls: available (from handle_push) + incoming (from handle_mqtt, fails but recorded)
    # + available (from handle_mqtt finally)
    topics = [
        str(call.kwargs.get("topic", call.args[0] if call.args else ""))
        for call in bridge.publish.call_args_list
    ]
    assert any("incoming" in t for t in topics)
    assert any("incoming_available" in t for t in topics)


@pytest.mark.asyncio
async def test_handle_mqtt_read_outgoing_still_notifies_on_failure(
    mailbox_component: tuple[MailboxComponent, AsyncMock],
    runtime_state: RuntimeState,
) -> None:
    component, bridge = mailbox_component
    runtime_state.enqueue_mailbox_message(b"test")

    async def flaky_publish(topic: str, *args: Any, **kwargs: Any) -> None:
        if str(topic).endswith("/incoming"):
            raise RuntimeError("boom")

    bridge.publish.side_effect = flaky_publish

    with pytest.raises(RuntimeError, match="boom"):
        # This will try to publish to 'incoming' (standard topic for read results)
        await component.handle_mqtt(
            make_route(Topic.MAILBOX, MailboxAction.READ.value),
            make_mqtt_msg(b""),
        )

    topics = [
        str(call.kwargs.get("topic", call.args[0] if call.args else ""))
        for call in bridge.publish.call_args_list
    ]
    assert any(t.endswith("/incoming") for t in topics)
    assert any(t.endswith("/outgoing_available") for t in topics)
