"""Extra coverage for mcubridge.services.mailbox."""

from typing import Any

from unittest.mock import AsyncMock, MagicMock

import pytest
from mcubridge.config.settings import RuntimeConfig
from mcubridge.services.mailbox import MailboxComponent
from mcubridge.state.context import create_runtime_state
from mcubridge.protocol.topics import Topic

from tests._helpers import make_route, make_mqtt_msg


@pytest.mark.asyncio
async def test_mailbox_handle_processed_fallback() -> None:
    import time
    import os

    config = RuntimeConfig(
        serial_shared_secret=b"secret_1234",
        file_system_root=f"/tmp/mcubridge-test-{os.getpid()}-{time.time_ns()}",
    )
    state = create_runtime_state(config)
    try:
        ctx = MagicMock()
        ctx.publish = AsyncMock()
        mb = MailboxComponent(config, state, ctx)

        # Payload too short or invalid for packet
        await mb.handle_processed(0, b"A")
        ctx.publish.assert_called_once()
        assert ctx.publish.call_args[1]["payload"] == b"A"
    finally:
        state.cleanup()


@pytest.mark.asyncio
async def test_mailbox_handle_read_truncation() -> None:
    import time
    import os

    config = RuntimeConfig(
        serial_shared_secret=b"secret_1234",
        file_system_root=f"/tmp/mcubridge-test-{os.getpid()}-{time.time_ns()}",
    )
    state = create_runtime_state(config)
    try:
        ctx = MagicMock()
        ctx.send_frame = AsyncMock(return_value=True)
        ctx.publish = AsyncMock()
        mb = MailboxComponent(config, state, ctx)

        state.enqueue_mailbox_message(b"A" * 100)
        await mb.handle_read(0, b"")

        # Verify sent payload is truncated to MAX_PAYLOAD_SIZE - 3 (61)
        args = ctx.send_frame.call_args[0]
        assert len(args[1]) <= 64  # 3 bytes msgpack prefix + 61 data
    finally:
        state.cleanup()


@pytest.mark.asyncio
async def test_mailbox_handle_read_send_fail(tmp_path: Any) -> None:
    config = RuntimeConfig(
        serial_shared_secret=b"secret_1234",
        file_system_root=tmp_path.as_posix(),
        mqtt_spool_dir=(tmp_path / "spool").as_posix(),
    )
    state = create_runtime_state(config)
    try:
        ctx = MagicMock()
        ctx.send_frame = AsyncMock(return_value=False)
        ctx.publish = AsyncMock()
        mb = MailboxComponent(config, state, ctx)

        msg = b"persistent"
        state.enqueue_mailbox_message(msg)
        await mb.handle_read(0, b"")
        # Message should be requeued at front
        assert state.pop_mailbox_message() == msg
    finally:
        state.cleanup()


@pytest.mark.asyncio
async def test_mailbox_handle_mqtt_edge_cases() -> None:
    import time
    import os

    config = RuntimeConfig(
        serial_shared_secret=b"secret_1234",
        file_system_root=f"/tmp/mcubridge-test-{os.getpid()}-{time.time_ns()}",
    )
    state = create_runtime_state(config)
    try:
        ctx = MagicMock()
        ctx.publish = AsyncMock()
        mb = MailboxComponent(config, state, ctx)

        # Unknown action
        await mb.handle_mqtt(
            make_route(Topic.MAILBOX, "unknown"), make_mqtt_msg(b"payload")
        )

        # Read from incoming queue
        state.enqueue_mailbox_incoming(b"inbound")
        await mb._handle_mqtt_read(None)
        ctx.publish.assert_called()
    finally:
        state.cleanup()


@pytest.mark.asyncio
async def test_mailbox_overflow_with_inbound() -> None:
    import time
    import os

    config = RuntimeConfig(
        serial_shared_secret=b"secret_1234",
        file_system_root=f"/tmp/mcubridge-test-{os.getpid()}-{time.time_ns()}",
    )
    state = create_runtime_state(config)
    try:
        ctx = MagicMock()
        ctx.send_frame = AsyncMock()
        ctx.publish = AsyncMock()
        mb = MailboxComponent(config, state, ctx)

        inbound = MagicMock()
        await mb._handle_outgoing_overflow(100, inbound)
        # Check for bridge-error property
        found_error = False
        for call in ctx.publish.call_args_list:
            if (
                call.kwargs.get("properties")
                and ("bridge-error", "mailbox") in call.kwargs["properties"]
            ):
                found_error = True
        assert found_error
    finally:
        state.cleanup()
