"""Extra coverage for MailboxComponent (SIL-2)."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import msgspec
import pytest

from mcubridge.config.settings import RuntimeConfig
from mcubridge.protocol.protocol import Status, Topic
from mcubridge.protocol.structures import MailboxPushPacket, TopicRoute
from mcubridge.services.mailbox import MailboxComponent
from mcubridge.services.serial_flow import SerialFlowController
from mcubridge.state.context import create_runtime_state


@pytest.fixture
def mailbox_comp(runtime_config: RuntimeConfig) -> MailboxComponent:
    state = create_runtime_state(runtime_config)
    serial_flow = AsyncMock(spec=SerialFlowController)
    serial_flow.send = AsyncMock(return_value=True)
    enqueue_mqtt = AsyncMock()
    return MailboxComponent(runtime_config, state, serial_flow, enqueue_mqtt)


@pytest.mark.asyncio
async def test_handle_push_overflow(mailbox_comp: MailboxComponent):
    # Set limit and fill queue
    mailbox_comp.state.mailbox_queue_limit = 1
    mailbox_comp.state.mailbox_incoming_queue.append(b"old-data")

    payload = msgspec.msgpack.encode(MailboxPushPacket(data=b"new-data"))
    ok = await mailbox_comp.handle_push(0, payload)
    assert ok is True
    assert len(mailbox_comp.state.mailbox_incoming_queue) == 1
    # In SIL-2 mailbox, we drop the NEW data on overflow to protect existing ones
    assert mailbox_comp.state.mailbox_incoming_queue[0] == b"old-data"


@pytest.mark.asyncio
async def test_handle_available_malformed(mailbox_comp: MailboxComponent):
    # Payload not empty
    ok = await mailbox_comp.handle_available(0, b"not-empty")
    # Returns the result of serial_flow.send(Status.MALFORMED)
    assert ok is True
    assert isinstance(mailbox_comp.serial_flow.send, AsyncMock)
    mailbox_comp.serial_flow.send.assert_called_with(Status.MALFORMED.value, b"")


@pytest.mark.asyncio
async def test_handle_mqtt_unknown_action(mailbox_comp: MailboxComponent):
    route = TopicRoute("br/mailbox/unknown", "br", Topic.MAILBOX, ("unknown",))
    from aiomqtt.message import Message

    msg = MagicMock(spec=Message)
    msg.payload = b""

    ok = await mailbox_comp.handle_mqtt(route, msg)
    assert ok is False
