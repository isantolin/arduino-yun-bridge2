"""Unit tests for mcubridge.services.mailbox (SIL-2)."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest
from mcubridge.config.settings import RuntimeConfig
from mcubridge.protocol import structures
from mcubridge.protocol.protocol import MailboxAction, Status
from mcubridge.services.base import BridgeContext
from mcubridge.services.mailbox import MailboxComponent
from mcubridge.state.context import RuntimeState, create_runtime_state
from mcubridge.protocol.topics import Topic
from tests._helpers import make_mqtt_msg, make_route


@pytest.fixture
def runtime_config() -> RuntimeConfig:
    import tempfile

    return RuntimeConfig(
        serial_port="/dev/null",
        mqtt_topic="br",
        file_system_root=tempfile.mkdtemp(prefix="mcubridge-test-fs-"),
        mqtt_spool_dir=tempfile.mkdtemp(prefix="mcubridge-test-spool-"),
        serial_shared_secret=b"s_e_c_r_e_t_mock",
    )


@pytest.fixture
def runtime_state(runtime_config: RuntimeConfig) -> RuntimeState:
    state = create_runtime_state(runtime_config)
    return state


@pytest.fixture
def ctx(runtime_config: RuntimeConfig, runtime_state: RuntimeState) -> MagicMock:
    c = MagicMock(spec=BridgeContext)
    c.config = runtime_config
    c.state = runtime_state
    c.serial_flow = MagicMock()
    c.serial_flow.send = AsyncMock(return_value=True)
    c.serial_flow.acknowledge = AsyncMock()
    c.mqtt_flow = MagicMock()
    c.mqtt_flow.publish = AsyncMock()
    c.mqtt_flow.enqueue_mqtt = AsyncMock()
    return c


@pytest.mark.asyncio
async def test_handle_push_stores_incoming_queue(
    ctx: MagicMock,
    runtime_config: RuntimeConfig,
    runtime_state: RuntimeState,
) -> None:
    component = MailboxComponent(runtime_config, runtime_state, ctx)
    data = b"hello from mcu"
    payload = structures.MailboxPushPacket(data=data).encode()

    await component.handle_push(0, payload)

    assert len(runtime_state.mailbox_incoming_queue) == 1
    # Pop to check content as BridgeQueue is not subscriptable
    popped = runtime_state.pop_mailbox_incoming()
    assert popped == data
    # Check that it published to MQTT
    assert ctx.mqtt_flow.publish.called


@pytest.mark.asyncio
async def test_handle_read_success_publishes_available(
    ctx: MagicMock,
    runtime_config: RuntimeConfig,
    runtime_state: RuntimeState,
) -> None:
    component = MailboxComponent(runtime_config, runtime_state, ctx)
    runtime_state.enqueue_mailbox_message(b"msg1")

    await component.handle_read(0, b"")

    ctx.serial_flow.send.assert_called_once()
    # Verify it published the new count
    ctx.mqtt_flow.publish.assert_called()
    args, kwargs = ctx.mqtt_flow.publish.call_args
    topic = kwargs.get("topic") or args[0]
    assert "outgoing_available" in topic


@pytest.mark.asyncio
async def test_handle_push_overflow_sends_error(
    ctx: MagicMock,
    runtime_config: RuntimeConfig,
    runtime_state: RuntimeState,
) -> None:
    component = MailboxComponent(runtime_config, runtime_state, ctx)
    # Use actual queue limits
    runtime_state.mailbox_queue_limit = 0

    payload = structures.MailboxPushPacket(data=b"too large").encode()
    await component.handle_push(0, payload)

    ctx.serial_flow.send.assert_called_once()
    assert ctx.serial_flow.send.call_args.args[0] == Status.ERROR.value


@pytest.mark.asyncio
async def test_handle_mqtt_write_enqueues_and_notifies(
    ctx: MagicMock,
    runtime_config: RuntimeConfig,
    runtime_state: RuntimeState,
) -> None:
    component = MailboxComponent(runtime_config, runtime_state, ctx)

    await component.handle_mqtt(
        make_route(Topic.MAILBOX, MailboxAction.WRITE.value),
        make_mqtt_msg(b"mqtt msg"),
    )

    assert len(runtime_state.mailbox_queue) == 1
    assert ctx.mqtt_flow.publish.called


@pytest.mark.asyncio
async def test_handle_mqtt_read_prefers_incoming_queue(
    ctx: MagicMock,
    runtime_config: RuntimeConfig,
    runtime_state: RuntimeState,
) -> None:
    component = MailboxComponent(runtime_config, runtime_state, ctx)

    # Message from MCU
    runtime_state.enqueue_mailbox_incoming(b"mcu-data")
    # Message to MCU
    runtime_state.enqueue_mailbox_message(b"linux-data")

    await component.handle_mqtt(
        make_route(Topic.MAILBOX, MailboxAction.READ.value),
        make_mqtt_msg(b""),
    )

    # Should have popped mcu-data first
    first_call = ctx.mqtt_flow.publish.call_args_list[0]
    args, kwargs = first_call
    payload = kwargs.get("payload") or args[1]
    assert payload == b"mcu-data"
    assert len(runtime_state.mailbox_incoming_queue) == 0
    assert len(runtime_state.mailbox_queue) == 1
