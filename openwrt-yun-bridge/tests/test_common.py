"""Tests for shared utilities."""

import pytest
from unittest.mock import MagicMock, patch

from yunbridge.common import (
    chunk_payload,
    deduplicate,
    get_uci_config,
    normalise_allowed_commands,
    get_default_config,
)


def test_chunk_payload():
    data = b"1234567890"
    assert chunk_payload(data, 5) == (b"12345", b"67890")
    assert chunk_payload(data, 3) == (b"123", b"456", b"789", b"0")
    assert chunk_payload(b"", 5) == ()

    with pytest.raises(ValueError):
        chunk_payload(data, 0)


def test_deduplicate():
    assert deduplicate([1, 2, 2, 3]) == (1, 2, 3)
    assert deduplicate([]) == ()
    assert deduplicate(["a", "b", "a"]) == ("a", "b")


def test_normalise_commands():
    cmds = ["cmd1 ", "CMD2", "cmd1", "*"]
    assert normalise_allowed_commands(cmds) == ("*",)

    cmds = ["cmd1", "CMD2"]
    assert normalise_allowed_commands(cmds) == ("cmd1", "cmd2")


def test_get_uci_config_missing_module():
    """Test fallback when uci module is not installed."""
    with patch.dict("sys.modules", {"uci": None}):
        config = get_uci_config()
        defaults = get_default_config()
        # Should match defaults exactly
        assert config == defaults


def test_get_uci_config_failure():
    """Test fallback when uci raises exception."""
    mock_uci = MagicMock()
    mock_uci.return_value.__enter__.side_effect = Exception("UCI Error")

    with patch.dict("sys.modules", {"uci": mock_uci}):
        config = get_uci_config()
        assert config == get_default_config()


def test_get_uci_config_success():
    """Test successful UCI read."""
    mock_module = MagicMock()
    mock_uci_class = MagicMock()
    mock_module.Uci = mock_uci_class
    mock_cursor = MagicMock()
    mock_uci_class.return_value.__enter__.return_value = mock_cursor

    # Simulate standard UCI dict return
    mock_cursor.get_all.return_value = {
        ".type": "general",
        "mqtt_host": "192.168.1.100",
        "debug": "1",
    }

    with patch.dict("sys.modules", {"uci": mock_module}):
        config = get_uci_config()
        assert config["mqtt_host"] == "192.168.1.100"
        assert config["debug"] == "1"
        # Ensure other defaults are present
        assert "serial_port" in config
