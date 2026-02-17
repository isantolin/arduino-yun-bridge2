"""Tests for MailboxComponent MCU/MQTT interactions."""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Coroutine
from typing import Any, Protocol

import msgspec
import pytest
from aiomqtt.message import Message
from mcubridge.config.settings import RuntimeConfig
from mcubridge.mqtt.messages import QueuedPublish
from mcubridge.protocol import protocol
from mcubridge.protocol.protocol import Command, MailboxAction, Status
from mcubridge.protocol.topics import (
    Topic,
    mailbox_incoming_available_topic,
    mailbox_outgoing_available_topic,
    topic_path,
)
from mcubridge.services.base import BridgeContext
from mcubridge.services.mailbox import MailboxComponent
from mcubridge.state.context import RuntimeState
from tests.test_constants import TEST_MSG_ID


class EnqueueHook(Protocol):
    def __call__(
        self,
        message: QueuedPublish,
        *,
        reply_context: Message | None = None,
    ) -> Awaitable[None]: ...


class DummyBridge(BridgeContext):
    def __init__(self, config: RuntimeConfig, state: RuntimeState) -> None:
        self.config = config
        self.state = state
        self.sent_frames: list[tuple[int, bytes]] = []
        self.published: list[QueuedPublish] = []
        self.send_frame_result = True
        self._enqueue_hook: EnqueueHook | None = None

    async def send_frame(self, command_id: int, payload: bytes = b"") -> bool:
        self.sent_frames.append((command_id, payload))
        return self.send_frame_result

    async def enqueue_mqtt(
        self,
        message: QueuedPublish,
        *,
        reply_context: Message | None = None,
    ) -> None:
        if self._enqueue_hook is not None:
            await self._enqueue_hook(
                message,
                reply_context=reply_context,
            )
            return
        self.published.append(message)

    async def publish(
        self,
        topic: str,
        payload: bytes | str,
        *,
        qos: int = 0,
        retain: bool = False,
        expiry: int | None = None,
        properties: tuple[tuple[str, str], ...] = (),
        content_type: str | None = None,
        reply_to: Message | None = None,
    ) -> None:
        payload_bytes = payload.encode("utf-8") if isinstance(payload, str) else payload
        message = QueuedPublish(
            topic_name=topic,
            payload=payload_bytes,
            qos=qos,
            retain=retain,
            content_type=content_type,
            message_expiry_interval=expiry,
            user_properties=properties,
        )
        await self.enqueue_mqtt(message, reply_context=reply_to)

    def set_enqueue_hook(self, hook: EnqueueHook | None) -> None:
        self._enqueue_hook = hook

    def is_command_allowed(self, command: str) -> bool:
        return True

    async def schedule_background(
        self,
        coroutine: Coroutine[Any, Any, None],
        *,
        name: str | None = None,
    ) -> asyncio.Task[Any]:  # pragma: no cover
        return asyncio.create_task(coroutine, name=name)


@pytest.fixture()
def mailbox_component(
    runtime_config: RuntimeConfig,
    runtime_state: RuntimeState,
) -> tuple[MailboxComponent, DummyBridge]:
    bridge = DummyBridge(runtime_config, runtime_state)
    component = MailboxComponent(runtime_config, runtime_state, bridge)
    return component, bridge


@pytest.fixture()
def mailbox_logger() -> logging.Logger:
    return logging.getLogger("test.mailbox")


def test_handle_processed_publishes_json(
    mailbox_component: tuple[MailboxComponent, DummyBridge],
    runtime_state: RuntimeState,
) -> None:
    component, bridge = mailbox_component
    payload = protocol.UINT16_STRUCT.build(TEST_MSG_ID)
    asyncio.run(component.handle_processed(payload))

    assert bridge.published
    message = bridge.published[-1]
    assert message.topic_name == topic_path(
        runtime_state.mqtt_topic_prefix,
        Topic.MAILBOX,
        "processed",
    )
    assert msgspec.json.decode(message.payload) == {"message_id": TEST_MSG_ID}


def test_handle_push_stores_incoming_queue(
    mailbox_component: tuple[MailboxComponent, DummyBridge],
    runtime_state: RuntimeState,
) -> None:
    component, bridge = mailbox_component
    payload = protocol.UINT16_STRUCT.build(5) + b"hello"
    result = asyncio.run(component.handle_push(payload))
    assert result is True
    assert list(runtime_state.mailbox_incoming_queue) == [b"hello"]

    topic_base = runtime_state.mqtt_topic_prefix
    assert [msg.topic_name for msg in bridge.published] == [
        topic_path(topic_base, Topic.MAILBOX, "incoming"),
        mailbox_incoming_available_topic(topic_base),
    ]
    assert bridge.published[1].payload == b"1"


def test_handle_push_overflow_sends_error(
    mailbox_component: tuple[MailboxComponent, DummyBridge],
    runtime_state: RuntimeState,
) -> None:
    component, bridge = mailbox_component
    runtime_state.mailbox_queue_limit = 0
    payload = protocol.UINT16_STRUCT.build(1) + b"A"
    result = asyncio.run(component.handle_push(payload))
    assert result is False
    assert bridge.sent_frames[-1][0] == Status.ERROR.value
    assert runtime_state.mailbox_incoming_overflow_events == 1


def test_handle_read_success_publishes_available(
    mailbox_component: tuple[MailboxComponent, DummyBridge],
    runtime_state: RuntimeState,
    mailbox_logger: logging.Logger,
) -> None:
    component, bridge = mailbox_component
    runtime_state.enqueue_mailbox_message(b"payload", mailbox_logger)

    result = asyncio.run(component.handle_read(b""))
    assert result is True

    command_id, payload = bridge.sent_frames[-1]
    assert command_id == Command.CMD_MAILBOX_READ_RESP.value
    assert payload == protocol.UINT16_STRUCT.build(7) + b"payload"

    assert bridge.published[-1].topic_name == (mailbox_outgoing_available_topic(runtime_state.mqtt_topic_prefix))
    assert bridge.published[-1].payload == b"0"


def test_handle_read_requeues_when_send_fails(
    mailbox_component: tuple[MailboxComponent, DummyBridge],
    runtime_state: RuntimeState,
    mailbox_logger: logging.Logger,
) -> None:
    component, bridge = mailbox_component
    runtime_state.enqueue_mailbox_message(b"fail", mailbox_logger)
    bridge.send_frame_result = False

    result = asyncio.run(component.handle_read(b""))
    assert result is False
    assert list(runtime_state.mailbox_queue) == [b"fail"]
    # No availability topic should be published on failure
    assert not bridge.published


def test_handle_mqtt_write_enqueues_and_notifies(
    mailbox_component: tuple[MailboxComponent, DummyBridge],
    runtime_state: RuntimeState,
) -> None:
    component, bridge = mailbox_component
    asyncio.run(component.handle_mqtt(MailboxAction.WRITE, b"mqtt"))

    assert list(runtime_state.mailbox_queue) == [b"mqtt"]
    assert bridge.published[-1].topic_name == (mailbox_outgoing_available_topic(runtime_state.mqtt_topic_prefix))
    assert bridge.published[-1].payload == b"1"


def test_handle_mqtt_write_overflow_signals_error(
    mailbox_component: tuple[MailboxComponent, DummyBridge],
    runtime_state: RuntimeState,
) -> None:
    component, bridge = mailbox_component
    runtime_state.mailbox_queue_limit = 0
    asyncio.run(component.handle_mqtt(MailboxAction.WRITE, b"boom"))

    assert not runtime_state.mailbox_queue
    assert bridge.sent_frames[-1][0] == Status.ERROR.value
    assert runtime_state.mailbox_outgoing_overflow_events == 1

    topics = [msg.topic_name for msg in bridge.published]
    overflow_topic = topic_path(
        runtime_state.mqtt_topic_prefix,
        Topic.MAILBOX,
        "errors",
    )
    assert topics[0] == mailbox_outgoing_available_topic(runtime_state.mqtt_topic_prefix)
    assert topics[1] == overflow_topic
    error_payload = msgspec.json.decode(bridge.published[1].payload)
    assert error_payload["event"] == "write_overflow"
    assert error_payload["overflow_events"] == 1


def test_handle_mqtt_read_prefers_incoming_queue(
    mailbox_component: tuple[MailboxComponent, DummyBridge],
    runtime_state: RuntimeState,
    mailbox_logger: logging.Logger,
) -> None:
    component, bridge = mailbox_component
    runtime_state.enqueue_mailbox_incoming(b"alpha", mailbox_logger)

    asyncio.run(component.handle_mqtt(MailboxAction.READ, b""))

    topic_base = runtime_state.mqtt_topic_prefix
    topics = [msg.topic_name for msg in bridge.published]
    assert topics == [
        topic_path(topic_base, Topic.MAILBOX, "incoming"),
        mailbox_incoming_available_topic(topic_base),
    ]
    assert bridge.published[0].payload == b"alpha"
    assert bridge.published[1].payload == b"0"


def test_handle_mqtt_read_drains_mailbox_queue(
    mailbox_component: tuple[MailboxComponent, DummyBridge],
    runtime_state: RuntimeState,
    mailbox_logger: logging.Logger,
) -> None:
    component, bridge = mailbox_component
    runtime_state.enqueue_mailbox_message(b"beta", mailbox_logger)

    asyncio.run(component.handle_mqtt(MailboxAction.READ, b""))

    topic_base = runtime_state.mqtt_topic_prefix
    topics = [msg.topic_name for msg in bridge.published]
    assert topics == [
        topic_path(topic_base, Topic.MAILBOX, "incoming"),
        mailbox_outgoing_available_topic(topic_base),
    ]
    assert bridge.published[0].payload == b"beta"
    assert bridge.published[1].payload == b"0"


def test_handle_mqtt_read_incoming_still_notifies_on_failure(
    mailbox_component: tuple[MailboxComponent, DummyBridge],
    runtime_state: RuntimeState,
    mailbox_logger: logging.Logger,
) -> None:
    component, bridge = mailbox_component
    runtime_state.enqueue_mailbox_incoming(b"gamma", mailbox_logger)

    async def flaky_enqueue(
        message: QueuedPublish,
        *,
        reply_context: Message | None = None,
    ) -> None:
        if message.topic_name.endswith("/incoming"):
            raise RuntimeError("boom")
        bridge.published.append(message)

    bridge.set_enqueue_hook(flaky_enqueue)

    with pytest.raises(RuntimeError):
        asyncio.run(component.handle_mqtt(MailboxAction.READ, b""))

    assert [msg.topic_name for msg in bridge.published] == [
        mailbox_incoming_available_topic(runtime_state.mqtt_topic_prefix)
    ]


def test_handle_mqtt_read_outgoing_still_notifies_on_failure(
    mailbox_component: tuple[MailboxComponent, DummyBridge],
    runtime_state: RuntimeState,
    mailbox_logger: logging.Logger,
) -> None:
    component, bridge = mailbox_component
    runtime_state.enqueue_mailbox_message(b"delta", mailbox_logger)

    async def flaky_enqueue(
        message: QueuedPublish,
        *,
        reply_context: Message | None = None,
    ) -> None:
        if message.topic_name.endswith("/incoming"):
            raise RuntimeError("boom")
        bridge.published.append(message)

    bridge.set_enqueue_hook(flaky_enqueue)

    with pytest.raises(RuntimeError):
        asyncio.run(component.handle_mqtt(MailboxAction.READ, b""))

    assert [msg.topic_name for msg in bridge.published] == [
        mailbox_outgoing_available_topic(runtime_state.mqtt_topic_prefix)
    ]
