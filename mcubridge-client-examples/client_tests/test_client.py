import pytest
from unittest.mock import MagicMock, AsyncMock, patch
from mcubridge_client import get_client, Topic
import ssl


def test_get_client_defaults() -> None:
    with patch("mcubridge_client.Client") as mock_client:
        client = get_client(host="1.2.3.4", port=1234)
        assert client is not None
        mock_client.assert_called_once()
        args, kwargs = mock_client.call_args
        assert kwargs["hostname"] == "1.2.3.4"
        assert kwargs["port"] == 1234


def test_topic_build_helpers() -> None:
    Topic.PREFIX = "br"
    assert str(Topic.build(Topic.DIGITAL, 13)) == "br/d/13"
    assert str(Topic.build(Topic.ANALOG, 0, "read")) == "br/a/0/read"
    assert str(Topic.build(Topic.FILE, "write", "test.txt")) == "br/file/write/test.txt"
    assert str(Topic.build(Topic.DATASTORE, "put", "key")) == "br/datastore/put/key"


def test_topic_matches() -> None:
    assert Topic.matches("br/d/13", "br/d/13")
    assert Topic.matches("br/d/+", "br/d/13")
    assert not Topic.matches("br/d/13", "br/d/14")


@pytest.mark.asyncio
async def test_client_context_manager() -> None:
    with patch("mcubridge_client.Client") as mock_client:
        mock_instance = mock_client.return_value
        mock_instance.__aenter__ = AsyncMock(return_value=mock_instance)
        mock_instance.__aexit__ = AsyncMock()
        
        async with get_client() as client:
            assert client is mock_instance
        
        mock_instance.__aenter__.assert_called_once()
        mock_instance.__aexit__.assert_called_once()
