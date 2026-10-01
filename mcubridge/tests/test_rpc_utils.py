import types
from typing import Any
from unittest.mock import MagicMock

from mcubridge.config import common
from mcubridge.protocol import protocol


def test_get_default_config_matches_constants():
    config = common.get_default_config()

    assert config["cloud_host"] == protocol.DEFAULT_CLOUD_HOST
    assert config["cloud_port"] == protocol.DEFAULT_CLOUD_PORT
    assert config["serial_port"] == protocol.DEFAULT_SERIAL_PORT
    assert config["serial_baud"] == protocol.DEFAULT_BAUDRATE
    assert config["serial_retry_attempts"] == protocol.DEFAULT_RETRY_LIMIT
    assert config["serial_retry_timeout"] == protocol.DEFAULT_SERIAL_RETRY_TIMEOUT
    assert config["serial_response_timeout"] == protocol.DEFAULT_SERIAL_RESPONSE_TIMEOUT


def test_get_uci_config_preserves_types():
    payload = {
        ".name": "general",
        ".type": "mcubridge",
        "serial_port": "uci-port",
        "cloud_host": "127.0.0.1",
        "cloud_port": 1883,
        "allowed_commands": ("ls", "echo"),
        "cloud_queue_limit": 42,
    }

    mock_cursor = MagicMock()
    mock_cursor.__enter__.return_value = mock_cursor
    mock_cursor.get_all.return_value = payload

    module = types.SimpleNamespace(
        Uci=MagicMock(return_value=mock_cursor),
        UciException=RuntimeError,
    )

    config = common.get_uci_config(uci_module=module)

    assert config["serial_port"] == "uci-port"
    # Raw tuple preserved in raw reader
    assert config["allowed_commands"] == ("ls", "echo")
    assert config["cloud_queue_limit"] == 42


def test_get_uci_config_falls_back_on_errors():
    mock_cursor = MagicMock()
    mock_cursor.__enter__.return_value = mock_cursor
    mock_cursor.get_all.side_effect = OSError("boom")

    module = types.SimpleNamespace(
        UCI=MagicMock(return_value=mock_cursor),
        UciException=OSError,
    )

    config = common.get_uci_config(uci_module=module)
    assert config == common.get_default_config()
