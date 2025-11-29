"""Tests for MailboxComponent MCU/MQTT interactions."""
from __future__ import annotations

import asyncio
import json
import logging
from typing import Any, Coroutine, Optional

import pytest

from yunbridge.common import pack_u16
from yunbridge.config.settings import RuntimeConfig
from yunbridge.protocol.topics import (
    Topic,
    mailbox_incoming_available_topic,
    mailbox_outgoing_available_topic,
    topic_path,
)
from yunbridge.mqtt import InboundMessage, PublishableMessage
from yunbridge.rpc.protocol import Command, Status
from yunbridge.services.components.base import BridgeContext
from yunbridge.services.components.mailbox import MailboxComponent
from yunbridge.state.context import RuntimeState


class DummyBridge(BridgeContext):
    def __init__(self, config: RuntimeConfig, state: RuntimeState) -> None:
        self.config = config
        self.state = state
        self.sent_frames: list[tuple[int, bytes]] = []
        self.published: list[PublishableMessage] = []
        self.send_frame_result = True

    async def send_frame(self, command_id: int, payload: bytes = b"") -> bool:
        self.sent_frames.append((command_id, payload))
        return self.send_frame_result

    async def enqueue_mqtt(
        self,
        message: PublishableMessage,
        *,
        reply_context: Optional[InboundMessage] = None,
    ) -> None:
        self.published.append(message)

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
    payload = pack_u16(0x1234)
    asyncio.run(component.handle_processed(payload))

    assert bridge.published
    message = bridge.published[-1]
    assert message.topic_name == topic_path(
        runtime_state.mqtt_topic_prefix,
        Topic.MAILBOX,
        "processed",
    )
    assert json.loads(message.payload) == {"message_id": 0x1234}


def test_handle_push_stores_incoming_queue(
    mailbox_component: tuple[MailboxComponent, DummyBridge],
    runtime_state: RuntimeState,
) -> None:
    component, bridge = mailbox_component
    payload = pack_u16(5) + b"hello"
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
    payload = pack_u16(1) + b"A"
    result = asyncio.run(component.handle_push(payload))
    assert result is False
    assert bridge.sent_frames[-1][0] == Status.ERROR.value


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
    assert payload == pack_u16(7) + b"payload"

    assert bridge.published[-1].topic_name == (
        mailbox_outgoing_available_topic(runtime_state.mqtt_topic_prefix)
    )
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
    asyncio.run(component.handle_mqtt_write(b"mqtt"))

    assert list(runtime_state.mailbox_queue) == [b"mqtt"]
    assert bridge.published[-1].topic_name == (
        mailbox_outgoing_available_topic(runtime_state.mqtt_topic_prefix)
    )
    assert bridge.published[-1].payload == b"1"


def test_handle_mqtt_read_prefers_incoming_queue(
    mailbox_component: tuple[MailboxComponent, DummyBridge],
    runtime_state: RuntimeState,
    mailbox_logger: logging.Logger,
) -> None:
    component, bridge = mailbox_component
    runtime_state.enqueue_mailbox_incoming(b"alpha", mailbox_logger)

    asyncio.run(component.handle_mqtt_read())

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

    asyncio.run(component.handle_mqtt_read())

    topic_base = runtime_state.mqtt_topic_prefix
    topics = [msg.topic_name for msg in bridge.published]
    assert topics == [
        topic_path(topic_base, Topic.MAILBOX, "incoming"),
        mailbox_outgoing_available_topic(topic_base),
    ]
    assert bridge.published[0].payload == b"beta"
    assert bridge.published[1].payload == b"0"
