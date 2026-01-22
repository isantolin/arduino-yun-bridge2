from unittest.mock import AsyncMock, MagicMock

import pytest
from aiomqtt import Client

# We are testing direct usage of aiomqtt since the custom Client shim is gone.
# The tests confirm we can mock aiomqtt.Client the same way daemon.py does.


@pytest.mark.asyncio
async def test_aiomqtt_context_manager_mocking():
    """Verify that we can mock the async context manager of aiomqtt."""
    mock_client_instance = AsyncMock(spec=BaseClient)

    # Setup the __aenter__ to return the mock instance itself
    mock_client_instance.__aenter__.return_value = mock_client_instance

    async with mock_client_instance as client:
        assert client is mock_client_instance
        await client.publish("test/topic", b"payload")

    mock_client_instance.publish.assert_awaited_once_with(
        "test/topic",
        b"payload",
    )
    mock_client_instance.__aexit__.assert_awaited_once()


@pytest.mark.asyncio
async def test_aiomqtt_messages_iterator_mocking():
    """Verify mocking of the messages async iterator."""
    mock_client = AsyncMock(spec=BaseClient)
    mock_messages = AsyncMock()

    # Mock messages property
    mock_client.messages = mock_messages

    # Mock async iterator behavior
    fake_msg = MagicMock()
    fake_msg.topic = "test/in"
    fake_msg.payload = b"123"

    # Setup __aiter__ to return an iterator that yields one item
    async def msg_gen():
        yield fake_msg

    mock_messages.__aiter__.side_effect = msg_gen

    received = []
    async for msg in mock_client.messages:
        received.append(msg)

    assert len(received) == 1
    assert received[0].topic == "test/in"
    assert received[0].payload == b"123"
