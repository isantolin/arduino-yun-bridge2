import sys
import types
import importlib

from typing import Self

import pytest

from mcubridge import common
from mcubridge import const
from mcubridge.rpc import protocol


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
    class FakeCursor:
        def __init__(self, payload: dict[str, object]) -> None:
            self._payload = payload

        def __enter__(self) -> Self:
            return self

        def __exit__(self, exc_type, exc, _tb) -> bool:
            return False

        def get_all(self, package: str, section: str) -> dict[str, object]:
            assert package == "mcubridge"
            assert section == "general"
            return self._payload

    payload = {
        ".name": "general",
        ".type": "mcubridge",
        "serial_port": "uci-port",
        "allowed_commands": ("ls", "echo"),
        "mqtt_queue_limit": 42,
    }

    module = types.SimpleNamespace(
        Uci=lambda: FakeCursor(payload),
        UciException=RuntimeError,
    )

    monkeypatch.setitem(sys.modules, "uci", module)
    importlib.reload(common)

    config = common.get_uci_config()

    assert config["serial_port"] == "uci-port"
    assert config["allowed_commands"] == "ls echo"
    assert config["mqtt_queue_limit"] == 42


def test_get_uci_config_falls_back_on_errors(monkeypatch: pytest.MonkeyPatch):
    class FakeCursor:
        def __enter__(self) -> Self:
            return self

        def __exit__(self, exc_type, exc, _tb) -> bool:
            return False

        def get_all(self, package: str, section: str) -> dict[str, object]:
            raise OSError("boom")

    module = types.SimpleNamespace(
        Uci=lambda: FakeCursor(),
        UciException=OSError,
    )
    monkeypatch.setitem(sys.modules, "uci", module)
    importlib.reload(common)

    fallback_called = False

    def fake_default() -> dict[str, str]:
        nonlocal fallback_called
        fallback_called = True
        return {"serial_port": "default"}

    monkeypatch.setattr(common, "get_default_config", fake_default)

    config = common.get_uci_config()

    assert fallback_called is True
    assert config == {"serial_port": "default"}
