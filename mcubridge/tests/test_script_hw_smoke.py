"""Unit tests for the hw-smoke CLI script."""

from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, patch, MagicMock

import aiomqtt
import pytest
from mcubridge.scripts.mcubridge_hw_smoke import main


def test_hw_smoke_success(runtime_config):
    with (
        patch(
            "mcubridge.scripts.mcubridge_hw_smoke.load_runtime_config",
            return_value=runtime_config,
        ),
        patch("aiomqtt.Client") as mock_client_cls,
        patch("sys.argv", ["mcubridge_hw_smoke", "--pin", "13", "--timeout", "0.1"]),
    ):

        mock_client = AsyncMock(spec=aiomqtt.Client)
        mock_client_cls.return_value.__aenter__.return_value = mock_client

        # Simulate incoming version message
        async def _mock_messages():
            msg = MagicMock(spec=aiomqtt.Message)
            msg.payload = b"1.2.3"
            yield msg

        mock_client.messages = _mock_messages()

        main()
        assert mock_client.publish.called


def test_hw_smoke_timeout(runtime_config):
    with (
        patch(
            "mcubridge.scripts.mcubridge_hw_smoke.load_runtime_config",
            return_value=runtime_config,
        ),
        patch("aiomqtt.Client") as mock_client_cls,
        patch("sys.argv", ["mcubridge_hw_smoke", "--timeout", "0.01"]),
        pytest.raises(SystemExit) as exc,
    ):

        mock_client = AsyncMock(spec=aiomqtt.Client)
        mock_client_cls.return_value.__aenter__.return_value = mock_client

        async def _mock_empty_messages():
            if False:
                yield  # empty generator
            await asyncio.sleep(1)

        mock_client.messages = _mock_empty_messages()

        main()
    assert exc.value.code == 1
