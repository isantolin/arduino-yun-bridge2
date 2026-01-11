"""Tests for shared utilities."""

from __future__ import annotations

from unittest.mock import MagicMock, patch

from mcubridge.common import (
    build_mqtt_connect_properties,
    build_mqtt_properties,
    encode_status_reason,
    get_default_config,
    get_uci_config,
    normalise_allowed_commands,
    parse_bool,
    parse_float,
    parse_int,
)
from mcubridge.mqtt.messages import QueuedPublish
from mcubridge.rpc import protocol


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


def test_get_uci_config_missing_section_returns_defaults() -> None:
    mock_module = MagicMock()
    mock_uci_class = MagicMock()
    mock_module.Uci = mock_uci_class
    mock_cursor = MagicMock()
    mock_uci_class.return_value.__enter__.return_value = mock_cursor
    mock_cursor.get_all.return_value = {}

    with patch.dict("sys.modules", {"uci": mock_module}):
        config = get_uci_config()
        assert config == get_default_config()


def test_get_uci_config_flattens_list_values_and_skips_internal_keys() -> None:
    mock_module = MagicMock()
    mock_uci_class = MagicMock()
    mock_module.Uci = mock_uci_class
    mock_cursor = MagicMock()
    mock_uci_class.return_value.__enter__.return_value = mock_cursor

    mock_cursor.get_all.return_value = {
        ".type": "general",
        "_meta": "ignore",
        "mqtt_host": ["example.com", 1883],
        "mqtt_tls": 0,
    }

    with patch.dict("sys.modules", {"uci": mock_module}):
        config = get_uci_config()
        assert config["mqtt_host"] == "example.com 1883"
        assert config["mqtt_tls"] == "0"
        assert ".type" not in config
        assert "_meta" not in config


def test_encode_status_reason_trims_to_max_payload() -> None:
    payload = encode_status_reason("x" * (protocol.MAX_PAYLOAD_SIZE + 50))
    assert len(payload) == protocol.MAX_PAYLOAD_SIZE


def test_build_mqtt_properties_returns_none_when_empty() -> None:
    message = QueuedPublish(
        topic_name=f"{protocol.MQTT_DEFAULT_TOPIC_PREFIX}/test",
        payload=b"hi",
    )
    assert build_mqtt_properties(message) is None


def test_build_mqtt_properties_populates_fields() -> None:
    message = QueuedPublish(
        topic_name=f"{protocol.MQTT_DEFAULT_TOPIC_PREFIX}/test",
        payload=b"hi",
        content_type="text/plain",
        payload_format_indicator=1,
        message_expiry_interval=10,
        response_topic=f"{protocol.MQTT_DEFAULT_TOPIC_PREFIX}/response",
        correlation_data=b"cid",
        user_properties=(("k", "v"),),
    )
    props = build_mqtt_properties(message)
    assert props is not None
    assert props.ContentType == "text/plain"
    assert props.PayloadFormatIndicator == 1
    assert props.MessageExpiryInterval == 10
    assert props.ResponseTopic == f"{protocol.MQTT_DEFAULT_TOPIC_PREFIX}/response"
    assert props.CorrelationData == b"cid"
    assert ("k", "v") in list(props.UserProperty)


def test_build_mqtt_connect_properties_sets_request_response_flags() -> None:
    props = build_mqtt_connect_properties()
    assert props.SessionExpiryInterval == 0
    assert props.RequestResponseInformation == 1
    assert props.RequestProblemInformation == 1
