"""Unit tests for the McuBridge Python Client library."""

import asyncio
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from aiomqtt.message import Message
from mcubridge_client import Bridge


@pytest.fixture
def mock_client():
    with patch("mcubridge_client.Client", autospec=True) as mock:
        yield mock


@pytest.mark.asyncio
async def test_client_connect_disconnect(mock_client) -> None:
    bridge = Bridge(host="127.0.0.1", port=1883, tls_context=None)
    await bridge.connect()

    mock_client.assert_called_once()

    await bridge.disconnect()
    # verify cleanup
    assert bridge._client is None


@pytest.mark.asyncio
async def test_client_digital_write(mock_client) -> None:
    bridge = Bridge(host="127.0.0.1", port=1883, tls_context=None)
    await bridge.connect()
    # The code calls publish on the client instance directly
    client_instance = mock_client.return_value
    client_instance.publish = AsyncMock()

    await bridge.digital_write(13, 1)

    # digital_write calls set_digital_mode first if not in cache, so multiple calls are expected
    assert client_instance.publish.called
    # Check if the last call was the actual digital write
    last_call = client_instance.publish.call_args_list[-1]
    assert "br/d/13" in last_call.args[0]
    assert last_call.args[1] == b"1"


@pytest.mark.asyncio
async def test_client_datastore_put(mock_client) -> None:
    bridge = Bridge(host="127.0.0.1", port=1883, tls_context=None)
    await bridge.connect()
    client_instance = mock_client.return_value
    client_instance.publish = AsyncMock()

    # datastore.put uses _publish_and_wait, which expects a response.
    # We need to simulate the response to avoid TimeoutError.
    async def simulate_response(*args, **kwargs):
        # The response topic for datastore/put/key is datastore/get/key
        topic = args[0]
        if "datastore/put/" in topic:
            key = topic.split("/")[-1]
            resp_topic = f"br/datastore/get/{key}"
            # Extract correlation data from properties
            props = kwargs.get("properties")
            correlation = getattr(props, "CorrelationData", None)

            # Create a mock response message
            msg = MagicMock(spec=Message)
            msg.topic = resp_topic
            msg.payload = b"OK"
            msg.qos = 0
            if correlation:
                msg.properties = MagicMock()
                msg.properties.CorrelationData = correlation

            # Inject into the bridge listener
            await bridge._handle_inbound_message(msg)

    client_instance.publish.side_effect = simulate_response

    await bridge.put("test_key", "test_value")

    assert client_instance.publish.called
    # Check if any call was for the datastore put
    put_call = next(c for c in client_instance.publish.call_args_list if "br/datastore/put/test_key" in c.args[0])
    assert put_call.args[1] == b"test_value"


@pytest.mark.asyncio
async def test_client_analog_read_timeout(mock_client) -> None:
    bridge = Bridge(host="127.0.0.1", port=1883, tls_context=None)
    await bridge.connect()

    # Simulate a timeout waiting for response
    with pytest.raises(asyncio.TimeoutError):
        await bridge.analog_read(0, timeout=0.1)


@pytest.mark.asyncio
async def test_client_file_write(mock_client) -> None:
    bridge = Bridge(host="127.0.0.1", port=1883, tls_context=None)
    await bridge.connect()
    client_instance = mock_client.return_value
    client_instance.publish = AsyncMock()

    await bridge.file_write("test.txt", "content")

    assert client_instance.publish.called
    last_call = client_instance.publish.call_args_list[-1]
    assert "br/file/write/test.txt" in last_call.args[0]
    assert last_call.args[1] == b"content"
