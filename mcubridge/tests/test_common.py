"""Tests for shared utilities."""

from __future__ import annotations

import importlib
import types
from unittest.mock import MagicMock, patch

from mcubridge.config import common

from mcubridge.protocol.structures import create_allowed_policy
from mcubridge.protocol import protocol


def test_normalise_commands():
    cmds = ["cmd1 ", "CMD2", "cmd1", "*"]
    policy = create_allowed_policy(cmds)
    assert list(policy.entries) == ["*"]

    cmds = ["cmd1", "CMD2"]
    policy = create_allowed_policy(cmds)
    assert list(policy.entries) == ["cmd1", "cmd2"]


def test_get_uci_config_success():
    """Test successful UCI read."""
    mock_module = MagicMock()

    mock_module.UciException = Exception
    mock_module.UCI = mock_module.Uci
    mock_cursor = MagicMock()
    mock_module.Uci.return_value.__enter__.return_value = mock_cursor

    # Simulate standard UCI dict return with minimum required fields
    mock_cursor.get_all.return_value = {
        ".type": "general",
        "mqtt_host": "127.0.0.1",
        "debug": "1",
        "serial_port": "/dev/ttyATH0",
        "mqtt_port": "1883",
    }

    with patch.dict("sys.modules", {"uci": mock_module}):
        importlib.reload(common)
        config = common.get_uci_config()
        assert config["mqtt_host"] == "127.0.0.1"
        assert config["debug"] == "1"
        # Ensure other defaults are present
        assert "serial_port" in config


def test_get_uci_config_missing_section_returns_defaults() -> None:
    mock_module = MagicMock()

    mock_module.UciException = Exception
    mock_module.UCI = mock_module.Uci
    mock_cursor = MagicMock()
    mock_module.Uci.return_value.__enter__.return_value = mock_cursor

    # Simulate missing section (empty dict or None depending on library, usually None or empty)
    # common.py checks `if not section`
    mock_cursor.get_all.return_value = {}

    with patch.dict("sys.modules", {"uci": mock_module}):
        importlib.reload(common)
        config = common.get_uci_config()
        assert config == common.get_default_config()


def test_get_uci_config_without_uci_class_returns_defaults() -> None:
    fake_module = types.ModuleType("uci")

    with patch.dict("sys.modules", {"uci": fake_module}):
        importlib.reload(common)
        config = common.get_uci_config()
        assert config == common.get_default_config()


def test_get_uci_config_without_get_all_returns_defaults() -> None:
    mock_uci_context = MagicMock()
    mock_cursor = MagicMock(spec=[])  # Has no get_all
    mock_uci_context.__enter__.return_value = mock_cursor
    mock_uci_context.__exit__.return_value = False

    mock_uci_class = MagicMock(return_value=mock_uci_context)

    fake_module = types.ModuleType("uci")
    setattr(fake_module, "Uci", mock_uci_class)

    with patch.dict("sys.modules", {"uci": fake_module}):
        importlib.reload(common)
        config = common.get_uci_config()
        assert config == common.get_default_config()


def test_get_uci_config_skips_internal_keys() -> None:
    mock_module = MagicMock()

    mock_module.UciException = Exception
    mock_module.UCI = mock_module.Uci
    mock_cursor = MagicMock()
    mock_module.Uci.return_value.__enter__.return_value = mock_cursor

    mock_cursor.get_all.return_value = {
        ".type": "general",
        "_meta": "ignore",
        "mqtt_host": ["example.com", 1883],
        "mqtt_tls": 0,
        "serial_port": "/dev/ttyATH0",
        "mqtt_port": "1883",
    }
    with patch.dict("sys.modules", {"uci": mock_module}):
        importlib.reload(common)
        config = common.get_uci_config()
        # Raw list preserved in the raw reader; flattening happens in settings.load_runtime_config
        assert config["mqtt_host"] == ["example.com", 1883]
        assert config["mqtt_tls"] == 0
        assert ".type" not in config
        assert "_meta" not in config


def test_encode_status_reason_trims_to_max_payload() -> None:
    reason = "x" * (protocol.MAX_PAYLOAD_SIZE + 50)
    payload = reason.encode("utf-8", errors="strict")[: protocol.MAX_PAYLOAD_SIZE]
    assert len(payload) == protocol.MAX_PAYLOAD_SIZE
