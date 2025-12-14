"""Tests for shared utilities."""

from unittest.mock import MagicMock, patch

from yunbridge.common import (
    get_uci_config,
    normalise_allowed_commands,
    get_default_config,
    parse_bool,
    parse_int,
    parse_float,
)


def test_parse_bool():
    assert parse_bool(True) is True
    assert parse_bool(False) is False
    assert parse_bool(1) is True
    assert parse_bool(0) is False
    assert parse_bool("1") is True
    assert parse_bool("0") is False
    assert parse_bool("true") is True
    assert parse_bool("False") is False
    assert parse_bool("yes") is True
    assert parse_bool("on") is True
    assert parse_bool("enabled") is True
    assert parse_bool(None) is False
    assert parse_bool("random") is False


def test_parse_int():
    assert parse_int(10, 0) == 10
    assert parse_int("10", 0) == 10
    assert parse_int("10.5", 0) == 10
    assert parse_int(None, 5) == 5
    assert parse_int("invalid", 5) == 5


def test_parse_float():
    assert parse_float(10.5, 0.0) == 10.5
    assert parse_float("10.5", 0.0) == 10.5
    assert parse_float(None, 1.0) == 1.0
    assert parse_float("invalid", 1.0) == 1.0


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
