import pytest
import asyncio
from unittest.mock import patch, AsyncMock
from mcubridge_client import get_client, dump_client_env


@pytest.mark.asyncio
async def test_smoke_connect_disconnect() -> None:
    """Verify that get_client returns a valid Client and it can 'connect'."""
    with patch("mcubridge_client.Client") as mock_client:
        mock_instance = mock_client.return_value
        mock_instance.__aenter__ = AsyncMock(return_value=mock_instance)
        mock_instance.__aexit__ = AsyncMock()
        
        async with get_client(host="127.0.0.1", port=1883) as client:
            assert client is mock_instance
            
        mock_instance.__aenter__.assert_called_once()
        mock_instance.__aexit__.assert_called_once()
