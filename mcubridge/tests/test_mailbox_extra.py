"""Extra edge-case tests for MailboxComponent (SIL-2)."""

from __future__ import annotations

import os
import time
from unittest.mock import AsyncMock, MagicMock

import pytest
from mcubridge.config.settings import RuntimeConfig
from mcubridge.services.mailbox import MailboxComponent
from mcubridge.state.context import create_runtime_state
from mcubridge.protocol.topics import Topic, TopicRoute
from tests._helpers import make_mqtt_msg


@pytest.mark.asyncio
async def test_mailbox_handle_processed_fallback() -> None:
    config = RuntimeConfig(
        serial_shared_secret=b"secret_1234",
        file_system_root=f"/tmp/mcubridge-test-{os.getpid()}-{time.time_ns()}",
    )
    state = create_runtime_state(config)
    try:
        ctx = MagicMock()
        ctx.serial_flow = MagicMock()
        ctx.mqtt_flow = MagicMock()
        ctx.mqtt_flow.publish = AsyncMock()

        comp = MailboxComponent(config, state, ctx)

        # Malformed payload fallback
        await comp.handle_processed(0, b"\xff\xff")

        ctx.mqtt_flow.publish.assert_called_once()
        args, kwargs = ctx.mqtt_flow.publish.call_args
        pub_payload = kwargs.get("payload") or args[1]
        assert pub_payload == b"\xff\xff"
    finally:
        state.cleanup()


@pytest.mark.asyncio
async def test_mailbox_handle_read_truncation() -> None:
    config = RuntimeConfig(
        serial_shared_secret=b"secret_1234",
        file_system_root=f"/tmp/mcubridge-test-{os.getpid()}-{time.time_ns()}",
    )
    state = create_runtime_state(config)
    try:
        ctx = MagicMock()
        ctx.serial_flow = MagicMock()
        ctx.serial_flow.send = AsyncMock(return_value=True)
        ctx.mqtt_flow = MagicMock()
        ctx.mqtt_flow.publish = AsyncMock()

        comp = MailboxComponent(config, state, ctx)

        # Very large payload
        large_data = b"x" * 1024
        state.enqueue_mailbox_message(large_data)

        await comp.handle_read(0, b"")

        ctx.serial_flow.send.assert_called_once()
        args, _ = ctx.serial_flow.send.call_args
        # Verify truncation in the sent frame
        sent_payload = args[1]
        assert len(sent_payload) < 1024
    finally:
        state.cleanup()


@pytest.mark.asyncio
async def test_mailbox_handle_read_send_fail() -> None:
    config = RuntimeConfig(
        serial_shared_secret=b"secret_1234",
        file_system_root=f"/tmp/mcubridge-test-{os.getpid()}-{time.time_ns()}",
    )
    state = create_runtime_state(config)
    try:
        ctx = MagicMock()
        ctx.serial_flow = MagicMock()
        ctx.serial_flow.send = AsyncMock(return_value=False)
        ctx.mqtt_flow = MagicMock()
        ctx.mqtt_flow.publish = AsyncMock()

        comp = MailboxComponent(config, state, ctx)

        data = b"hello"
        state.enqueue_mailbox_message(data)

        result = await comp.handle_read(0, b"")

        assert result is False
        # Should have requeued
        assert len(state.mailbox_queue) == 1
    finally:
        state.cleanup()


@pytest.mark.asyncio
async def test_mailbox_handle_mqtt_edge_cases() -> None:
    config = RuntimeConfig(
        serial_shared_secret=b"secret_1234",
        file_system_root=f"/tmp/mcubridge-test-{os.getpid()}-{time.time_ns()}",
    )
    state = create_runtime_state(config)
    try:
        ctx = MagicMock()
        ctx.serial_flow = MagicMock()
        ctx.mqtt_flow = MagicMock()
        ctx.mqtt_flow.publish = AsyncMock()

        comp = MailboxComponent(config, state, ctx)

        # Unknown action
        route = TopicRoute("br/m/unknown", "br", Topic.MAILBOX, ("unknown",))
        await comp.handle_mqtt_read(route, make_mqtt_msg(b""))

        assert not ctx.mqtt_flow.publish.called
    finally:
        state.cleanup()


@pytest.mark.asyncio
async def test_mailbox_overflow_with_inbound() -> None:
    config = RuntimeConfig(
        serial_shared_secret=b"secret_1234",
        file_system_root=f"/tmp/mcubridge-test-{os.getpid()}-{time.time_ns()}",
    )
    state = create_runtime_state(config)
    try:
        ctx = MagicMock()
        ctx.serial_flow = MagicMock()
        ctx.serial_flow.send = AsyncMock(return_value=True)
        ctx.mqtt_flow = MagicMock()
        ctx.mqtt_flow.publish = AsyncMock()

        comp = MailboxComponent(config, state, ctx)

        # Force overflow
        state.mailbox_queue_limit = 0

        inbound = make_mqtt_msg(b"data")
        inbound.properties = MagicMock()
        inbound.properties.ResponseTopic = "reply"
        route = MagicMock(spec=TopicRoute)

        await comp.handle_mqtt_write(route, inbound)

        assert ctx.mqtt_flow.publish.called
        # Should include reply_to context
        assert any(call.kwargs.get("reply_to") is inbound for call in ctx.mqtt_flow.publish.call_args_list)
    finally:
        state.cleanup()
