# pyright: reportPrivateUsage=false
from __future__ import annotations

from typing import Any, cast
from unittest.mock import AsyncMock, MagicMock

import pytest
import msgspec
from mcubridge.services.mailbox import MailboxComponent
from mcubridge.state.context import create_runtime_state
from mcubridge.protocol.structures import MailboxPushPacket, TopicRoute
from mcubridge.protocol.topics import Topic
from aiomqtt.message import Message


@pytest.fixture
def mailbox_comp(runtime_config: Any):
    state = create_runtime_state(runtime_config)
    comp = MailboxComponent(
        config=runtime_config,
        state=state,
        serial_flow=AsyncMock(),
        mqtt_flow=AsyncMock(),
    )
    return comp


@pytest.mark.asyncio
async def test_handle_processed_malformed(mailbox_comp: MailboxComponent):
    # Short payload
    await mailbox_comp.handle_processed(0, b"\x01")
    assert cast(Any, mailbox_comp.mqtt_flow.publish).called


@pytest.mark.asyncio
async def test_handle_push_overflow(mailbox_comp: MailboxComponent):
    # Mock queue to fail append
    mailbox_comp.state.mailbox_incoming_queue = MagicMock()
    mailbox_comp.state.mailbox_incoming_queue.append.return_value = type(
        "Event", (), {"success": False}
    )()

    payload = msgspec.msgpack.encode(MailboxPushPacket(data=b"data"))
    ok = await mailbox_comp.handle_push(0, payload)
    assert ok is False
    assert cast(Any, mailbox_comp.serial_flow.send).called


@pytest.mark.asyncio
async def test_handle_available_malformed(mailbox_comp: MailboxComponent):
    # Payload not empty
    ok = await mailbox_comp.handle_available(0, b"not-empty")
    assert ok is False
    assert cast(Any, mailbox_comp.serial_flow.send).called


@pytest.mark.asyncio
async def test_handle_read_empty(mailbox_comp: MailboxComponent):
    # Pop from empty queue
    ok = await mailbox_comp.handle_read(0, b"")
    assert ok is True  # Sends empty response


@pytest.mark.asyncio
async def test_handle_mqtt_unknown_action(mailbox_comp: MailboxComponent):
    route = TopicRoute(raw="", prefix="br", topic=Topic.MAILBOX, segments=("unknown",))
    msg = Message("br/mailbox/unknown", b"", 0, False, False, None)
    ok = await mailbox_comp.handle_mqtt(route, msg)
    assert ok is True


@pytest.mark.asyncio
async def test_handle_outgoing_overflow(mailbox_comp: MailboxComponent):
    # Mock queue to fail append
    mailbox_comp.state.mailbox_queue = MagicMock()
    mailbox_comp.state.mailbox_queue.append.return_value = type(
        "Event", (), {"success": False}
    )()
    mailbox_comp.state.mailbox_queue_limit = 10
    mailbox_comp.state.mailbox_queue_bytes_limit = 100
    mailbox_comp.state.mailbox_queue_bytes = 0
    mailbox_comp.state.mailbox_outgoing_overflow_events = 0

    await mailbox_comp._handle_mqtt_write(b"too-much-data")
    assert cast(Any, mailbox_comp.serial_flow.send).called
    assert cast(Any, mailbox_comp.mqtt_flow.publish).called  # Error topic
