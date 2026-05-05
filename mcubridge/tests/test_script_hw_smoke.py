"""Unit tests for the hw-smoke CLI script."""

from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, patch, MagicMock
from typing import Any
import importlib.util
from pathlib import Path
import sys

import aiomqtt
import pytest

# Dynamically import the script since its name contains dashes
script_path = Path(__file__).parent.parent / "scripts" / "mcubridge-hw-smoke.py"
spec = importlib.util.spec_from_file_location("mcubridge_hw_smoke", str(script_path))
if spec is None or spec.loader is None:
    raise ImportError("Could not load mcubridge-hw-smoke.py")
mcubridge_hw_smoke = importlib.util.module_from_spec(spec)
sys.modules["mcubridge_hw_smoke"] = mcubridge_hw_smoke
spec.loader.exec_module(mcubridge_hw_smoke)
main = getattr(mcubridge_hw_smoke, "main")


def test_hw_smoke_success(runtime_config: Any) -> None:
    runtime_config.mqtt_tls = False
    with (
        patch(
            "mcubridge_hw_smoke.load_runtime_config",
            return_value=runtime_config,
        ),
        patch("aiomqtt.Client") as mock_client_cls,
        patch("sys.argv", ["mcubridge_hw_smoke", "--pin", "13", "--timeout", "0.1"]),
    ):

        mock_client = AsyncMock()
        mock_client_cls.return_value.__aenter__.return_value = mock_client

        # Simulate incoming version message
        async def _mock_messages() -> Any:
            msg = MagicMock(spec=aiomqtt.Message)
            msg.payload = b"1.2.3"
            yield msg

        mock_client.messages = _mock_messages()

        main()
        assert mock_client.publish.called


def test_hw_smoke_timeout(runtime_config: Any) -> None:
    runtime_config.mqtt_tls = False
    with (
        patch(
            "mcubridge_hw_smoke.load_runtime_config",
            return_value=runtime_config,
        ),
        patch("aiomqtt.Client") as mock_client_cls,
        patch("sys.argv", ["mcubridge_hw_smoke", "--timeout", "0.01"]),
        pytest.raises(SystemExit) as exc,
    ):

        mock_client = AsyncMock()
        mock_client_cls.return_value.__aenter__.return_value = mock_client

        async def _mock_empty_messages() -> Any:
            if False:
                yield  # empty generator
            await asyncio.sleep(1)

        mock_client.messages = _mock_empty_messages()

        main()
    assert exc.value.code == 1
