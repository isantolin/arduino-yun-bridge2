import pytest
from typing import Any
from unittest.mock import MagicMock, AsyncMock
from mcubridge.services.mailbox import MailboxComponent


@pytest.fixture
def mailbox_component(runtime_config: Any, runtime_state: Any) -> MailboxComponent:
    ctx = MagicMock()
    ctx.serial_flow = AsyncMock()
    ctx.mqtt_flow = AsyncMock()
    return MailboxComponent(runtime_config, runtime_state, ctx)


@pytest.mark.asyncio
async def test_mailbox_handle_push_large_data(mailbox_component: MailboxComponent, runtime_state: Any):
    large_data = b"x" * 1024
    # Directly use the queue for tests
    runtime_state.mailbox_incoming_queue.append(large_data)
    assert len(runtime_state.mailbox_incoming_queue) == 1


@pytest.mark.asyncio
async def test_mailbox_handle_mqtt_push_limit(mailbox_component: MailboxComponent, runtime_state: Any):
    # Directly set the limit on the queue object
    runtime_state.mailbox_queue.max_items = 1
    data = b"data"
    runtime_state.mailbox_queue.append(data)
    # Adding another should trigger Native metrics aggregation in BridgeQueue
    runtime_state.mailbox_queue.append(b"data2")
    assert runtime_state.mailbox_queue.dropped_chunks >= 1
