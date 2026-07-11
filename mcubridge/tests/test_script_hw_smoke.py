"""Unit tests for the hw-smoke CLI script."""

from __future__ import annotations

from unittest.mock import patch, MagicMock, AsyncMock
from typing import Any
import importlib.util
from pathlib import Path
import sys
import pytest

# Dynamically import the script since its name contains underscores (normalized)
script_path = Path(__file__).parent.parent / "scripts" / "mcubridge_hw_smoke.py"
spec = importlib.util.spec_from_file_location("mcubridge_hw_smoke", str(script_path))
if spec is None or spec.loader is None:
    raise ImportError("Could not load mcubridge_hw_smoke.py")
mcubridge_hw_smoke = importlib.util.module_from_spec(spec)
sys.modules["mcubridge_hw_smoke"] = mcubridge_hw_smoke
spec.loader.exec_module(mcubridge_hw_smoke)
main = getattr(mcubridge_hw_smoke, "main")


def test_hw_smoke_success(runtime_config: Any) -> None:
    with (
        patch(
            "mcubridge_hw_smoke.load_runtime_config",
            return_value=runtime_config,
        ),
        patch("mcubridge_hw_smoke.Channel") as mock_channel_cls,
        patch("mcubridge_hw_smoke.LocalBridgeStub") as mock_stub_cls,
        patch("sys.argv", ["mcubridge_hw_smoke", "--pin", "13", "--timeout", "0.1"]),
    ):
        mock_stub = MagicMock()
        mock_stub_cls.return_value = mock_stub
        mock_stub.Publish = AsyncMock()
        main()
        mock_channel_cls.assert_called_once_with(path="/var/run/mcubridge.sock")
        assert mock_stub.Publish.call_count >= 1


def test_hw_smoke_timeout(runtime_config: Any) -> None:
    with (
        patch(
            "mcubridge_hw_smoke.load_runtime_config",
            return_value=runtime_config,
        ),
        patch("mcubridge_hw_smoke.Channel") as mock_channel_cls,
        patch("sys.argv", ["mcubridge_hw_smoke", "--timeout", "0.01"]),
        pytest.raises(SystemExit) as exc,
    ):
        mock_channel_cls.side_effect = Exception("Connection refused")
        main()
    assert exc.value.code == 1
