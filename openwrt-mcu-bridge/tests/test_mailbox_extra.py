"""Extra coverage for mcubridge.services.mailbox."""

from unittest.mock import AsyncMock, MagicMock

import pytest
from mcubridge.config.settings import RuntimeConfig
from mcubridge.services.mailbox import MailboxComponent
from mcubridge.state.context import create_runtime_state


@pytest.mark.asyncio
async def test_mailbox_handle_processed_fallback() -> None:
    config = RuntimeConfig(serial_shared_secret=b"secret_1234")
    state = create_runtime_state(config)
    ctx = MagicMock()
    ctx.publish = AsyncMock()
    mb = MailboxComponent(config, state, ctx)

    # Payload too short or invalid for packet
    await mb.handle_processed(b"A")
    ctx.publish.assert_called_once()
    assert ctx.publish.call_args[1]["payload"] == b"A"


@pytest.mark.asyncio
async def test_mailbox_handle_read_truncation() -> None:
    config = RuntimeConfig(serial_shared_secret=b"secret_1234")
    state = create_runtime_state(config)
    ctx = MagicMock()
    ctx.send_frame = AsyncMock(return_value=True)
    ctx.publish = AsyncMock()
    mb = MailboxComponent(config, state, ctx)

    state.enqueue_mailbox_message(b"A" * 100, MagicMock())
    await mb.handle_read(b"")

    # Verify sent payload is truncated to MAX_PAYLOAD_SIZE - 2 (62)
    args = ctx.send_frame.call_args[0]
    assert len(args[1]) <= 64 # 2 bytes prefix + 62 data


@pytest.mark.asyncio
async def test_mailbox_handle_read_send_fail() -> None:
    config = RuntimeConfig(serial_shared_secret=b"secret_1234")
    state = create_runtime_state(config)
    ctx = MagicMock()
    ctx.send_frame = AsyncMock(return_value=False)
    ctx.publish = AsyncMock()
    mb = MailboxComponent(config, state, ctx)

    msg = b"persistent"
    state.enqueue_mailbox_message(msg, MagicMock())
    await mb.handle_read(b"")
    # Message should be requeued at front
    assert state.pop_mailbox_message() == msg


@pytest.mark.asyncio
async def test_mailbox_handle_mqtt_edge_cases() -> None:
    config = RuntimeConfig(serial_shared_secret=b"secret_1234")
    state = create_runtime_state(config)
    ctx = MagicMock()
    ctx.publish = AsyncMock()
    mb = MailboxComponent(config, state, ctx)

    # Unknown action
    await mb.handle_mqtt("unknown", b"payload")

    # Read from incoming queue
    state.enqueue_mailbox_incoming(b"inbound", MagicMock())
    await mb._handle_mqtt_read(None)
    ctx.publish.assert_called()


@pytest.mark.asyncio
async def test_mailbox_overflow_with_inbound() -> None:
    config = RuntimeConfig(serial_shared_secret=b"secret_1234")
    state = create_runtime_state(config)
    ctx = MagicMock()
    ctx.send_frame = AsyncMock()
    ctx.publish = AsyncMock()
    mb = MailboxComponent(config, state, ctx)

    inbound = MagicMock()
    await mb._handle_outgoing_overflow(100, inbound)
    # Check for bridge-error property
    found_error = False
    for call in ctx.publish.call_args_list:
        if call.kwargs.get("properties") and ("bridge-error", "mailbox") in call.kwargs["properties"]:
            found_error = True
    assert found_error
