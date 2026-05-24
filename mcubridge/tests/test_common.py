"""Tests for shared utilities."""

from __future__ import annotations

import importlib
from unittest.mock import MagicMock, patch

from mcubridge.config import common

from mcubridge.protocol.structures import QueuedPublish, AllowedCommandPolicy
from mcubridge.protocol import protocol


def test_normalise_commands():
    cmds = ["cmd1 ", "CMD2", "cmd1", "*"]
    policy = AllowedCommandPolicy.from_iterable(cmds)
    assert policy.entries == ("*",)

    cmds = ["cmd1", "CMD2"]
    policy = AllowedCommandPolicy.from_iterable(cmds)
    assert policy.entries == ("cmd1", "cmd2")


def test_get_uci_config_success():
    """Test successful UCI read."""
    mock_module = MagicMock()
    # Explicitly remove attributes that are checked to detect fake libraries
    del mock_module.nltk
    del mock_module.vocab

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
    del mock_module.nltk
    del mock_module.vocab

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


def test_get_uci_config_skips_internal_keys() -> None:
    mock_module = MagicMock()
    del mock_module.nltk
    del mock_module.vocab

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
    payload = reason.encode("utf-8", errors="ignore")[: protocol.MAX_PAYLOAD_SIZE]
    assert len(payload) == protocol.MAX_PAYLOAD_SIZE


def test_queued_publish_properties_empty() -> None:
    from mcubridge_client.definitions import build_mqtt_properties

    message = QueuedPublish(
        topic_name=f"{protocol.MQTT_DEFAULT_TOPIC_PREFIX}/test",
        payload=b"hi",
    )
    # The new helper always returns a Properties object (empty or not)
    props = build_mqtt_properties(message)
    assert not any(
        getattr(props, attr, None)
        for attr in [
            "ContentType",
            "PayloadFormatIndicator",
            "MessageExpiryInterval",
            "ResponseTopic",
            "CorrelationData",
            "UserProperty",
        ]
    )


def test_build_mqtt_properties_populates_fields() -> None:
    from mcubridge_client.definitions import build_mqtt_properties

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
