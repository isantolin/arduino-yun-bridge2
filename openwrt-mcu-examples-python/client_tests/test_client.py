"""Unit tests for the McuBridge Python Client library."""

import asyncio
import ssl
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
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
    instance = mock_client.return_value.__aenter__.return_value
    
    await bridge.disconnect()
    # verify cleanup
    assert bridge._client is None


@pytest.mark.asyncio
async def test_client_digital_write(mock_client) -> None:
    bridge = Bridge(host="127.0.0.1", port=1883, tls_context=None)
    await bridge.connect()
    instance = mock_client.return_value.__aenter__.return_value
    instance.publish = AsyncMock()

    await bridge.digital_write(13, 1)
    
    # Check if correct topic and payload were used
    instance.publish.assert_called_once()
    call_args = instance.publish.call_args
    assert "br/d/13" in call_args.args[0]
    assert call_args.args[1] == b"1"


@pytest.mark.asyncio
async def test_client_datastore_put(mock_client) -> None:
    bridge = Bridge(host="127.0.0.1", port=1883, tls_context=None)
    await bridge.connect()
    instance = mock_client.return_value.__aenter__.return_value
    instance.publish = AsyncMock()

    await bridge.put("test_key", "test_value")
    
    instance.publish.assert_called_once()
    assert "br/datastore/put/test_key" in instance.publish.call_args.args[0]
    assert instance.publish.call_args.args[1] == b"test_value"


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
    instance = mock_client.return_value.__aenter__.return_value
    instance.publish = AsyncMock()

    await bridge.file_write("test.txt", "content")
    
    instance.publish.assert_called_once()
    assert "br/file/write/test.txt" in instance.publish.call_args.args[0]
    assert instance.publish.call_args.args[1] == b"content"
