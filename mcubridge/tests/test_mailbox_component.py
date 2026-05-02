"""Unit tests for MailboxComponent MCU/MQTT behaviour (SIL-2)."""

from __future__ import annotations

from typing import Any, cast
from unittest.mock import AsyncMock, MagicMock

import msgspec
import pytest
from aiomqtt.message import Message

from mcubridge.config.settings import RuntimeConfig
from mcubridge.protocol import structures
from mcubridge.protocol.protocol import Command, MailboxAction, Status, Topic
from mcubridge.protocol.structures import TopicRoute
from mcubridge.services.mailbox import MailboxComponent
from mcubridge.services.serial_flow import SerialFlowController
from mcubridge.state.context import RuntimeState, create_runtime_state


@pytest.fixture
def mailbox_component(
    runtime_config: RuntimeConfig,
) -> MailboxComponent:
    state = create_runtime_state(runtime_config)
    state.mqtt_topic_prefix = "br"

    serial_flow = AsyncMock(spec=SerialFlowController)
    serial_flow.acknowledge = AsyncMock()
    serial_flow.send = AsyncMock(return_value=True)
    enqueue_mqtt = AsyncMock()

    return MailboxComponent(runtime_config, state, serial_flow, enqueue_mqtt)


@pytest.mark.asyncio
async def test_handle_push_success(mailbox_component: MailboxComponent) -> None:
    # MCU pushes data to Linux
    pkt = structures.MailboxPushPacket(data=b"mcu-data")
    payload = msgspec.msgpack.encode(pkt)

    await mailbox_component.handle_push(0, payload)

    assert len(mailbox_component.state.mailbox_incoming_queue) == 1
    assert mailbox_component.state.mailbox_incoming_queue[0] == b"mcu-data"
    
    mailbox_component.enqueue_mqtt.assert_called()


@pytest.mark.asyncio
async def test_handle_mqtt_logic(
    mailbox_component: MailboxComponent, runtime_state: Any
):
    # Test write via MQTT
    route = TopicRoute(
        raw="br/mailbox/write",
        prefix="br",
        topic=Topic.MAILBOX,
        segments=(MailboxAction.WRITE.value,),
    )
    msg = Message("test/topic", b"mcu-data", 0, False, mid=1, properties=None)
    await mailbox_component.handle_mqtt(route, msg)
    
    assert len(runtime_state.mailbox_queue) == 1
    assert runtime_state.mailbox_queue[0] == b"mcu-data"


@pytest.mark.asyncio
async def test_handle_read_request_success(
    mailbox_component: MailboxComponent,
) -> None:
    # Setup data in queue
    mailbox_component.state.mailbox_queue.append(b"linux-data")

    await mailbox_component.handle_read(0, b"")

    mailbox_component.serial_flow.send.assert_called()
    call_args = mailbox_component.serial_flow.send.call_args
    assert call_args.args[0] == Command.CMD_MAILBOX_READ_RESP.value
    # Result payload should contain the data
    assert b"linux-data" in call_args.args[1]
