import importlib
import sys
import types
from typing import Any
from unittest.mock import MagicMock

import pytest
from mcubridge.config import common, const
from mcubridge.protocol import protocol


def test_get_default_config_matches_constants():
    config = common.get_default_config()

    assert config["mqtt_host"] == const.DEFAULT_MQTT_HOST
    assert config["mqtt_port"] == const.DEFAULT_MQTT_PORT
    assert config["serial_port"] == const.DEFAULT_SERIAL_PORT
    assert config["serial_baud"] == protocol.DEFAULT_BAUDRATE
    assert config["serial_retry_attempts"] == protocol.DEFAULT_RETRY_LIMIT
    assert config["serial_retry_timeout"] == const.DEFAULT_SERIAL_RETRY_TIMEOUT
    assert config["serial_response_timeout"] == const.DEFAULT_SERIAL_RESPONSE_TIMEOUT


def test_get_uci_config_preserves_types(monkeypatch: pytest.MonkeyPatch):
    payload = {
        ".name": "general",
        ".type": "mcubridge",
        "serial_port": "uci-port",
        "mqtt_host": "127.0.0.1",
        "mqtt_port": 1883,
        "allowed_commands": ("ls", "echo"),
        "mqtt_queue_limit": 42,
    }

    mock_cursor = MagicMock()
    mock_cursor.__enter__.return_value = mock_cursor
    mock_cursor.get_all.return_value = payload

    module = types.SimpleNamespace(
        Uci=MagicMock(return_value=mock_cursor),
        UciException=RuntimeError,
    )

    monkeypatch.setitem(sys.modules, "uci", module)
    importlib.reload(common)

    config = common.get_uci_config()

    assert config["serial_port"] == "uci-port"
    # Raw tuple preserved in raw reader
    assert config["allowed_commands"] == ("ls", "echo")
    assert config["mqtt_queue_limit"] == 42


def test_get_uci_config_falls_back_on_errors(monkeypatch: pytest.MonkeyPatch):
    mock_cursor = MagicMock()
    mock_cursor.__enter__.return_value = mock_cursor
    mock_cursor.get_all.side_effect = OSError("boom")

    module = types.SimpleNamespace(
        UCI=MagicMock(return_value=mock_cursor),
        UciException=OSError,
    )
    monkeypatch.setitem(sys.modules, "uci", module)
    importlib.reload(common)

    fallback_called = False

    def fake_default() -> dict[str, Any]:
        nonlocal fallback_called
        fallback_called = True
        return {
            "serial_port": "default",
            "serial_baud": 115200,
            "serial_safe_baud": 115200,
            "serial_retry_attempts": 5,
            "serial_retry_timeout": 10.0,
            "serial_response_timeout": 20.0,
            "mqtt_host": "127.0.0.1",
            "mqtt_port": 1883,
            "debug": False,
        }

    monkeypatch.setattr(common, "get_default_config", fake_default)

    config = common.get_uci_config()

    assert fallback_called is True
    assert config["serial_port"] == "default"
    assert config["serial_baud"] == 115200
