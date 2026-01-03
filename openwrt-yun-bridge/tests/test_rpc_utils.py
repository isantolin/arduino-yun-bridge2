import sys
import types

from typing import Self

import pytest

from yunbridge import common as utils
from yunbridge import const
from yunbridge.rpc import protocol


def test_get_default_config_matches_constants():
    config = utils.get_default_config()

    assert config["mqtt_host"] == const.DEFAULT_MQTT_HOST
    assert config["mqtt_port"] == str(const.DEFAULT_MQTT_PORT)
    assert config["serial_port"] == const.DEFAULT_SERIAL_PORT
    assert config["serial_baud"] == str(protocol.DEFAULT_BAUDRATE)
    assert config["serial_retry_attempts"] == str(protocol.DEFAULT_RETRY_LIMIT)
    assert config["serial_retry_timeout"] == str(const.DEFAULT_SERIAL_RETRY_TIMEOUT)
    assert config["serial_response_timeout"] == str(
        const.DEFAULT_SERIAL_RESPONSE_TIMEOUT
    )
    assert all(isinstance(value, str) for value in config.values())


def test_get_uci_config_stringifies_values(monkeypatch: pytest.MonkeyPatch):
    class FakeCursor:
        def __init__(self, payload: dict[str, object]) -> None:
            self._payload = payload

        def __enter__(self) -> Self:
            return self

        def __exit__(self, exc_type, exc, _tb) -> bool:
            return False

        def get_all(self, package: str, section: str) -> dict[str, object]:
            assert package == "yunbridge"
            assert section == "general"
            return self._payload

    payload = {
        ".name": "general",
        ".type": "yunbridge",
        "serial_port": "uci-port",
        "allowed_commands": ("ls", "echo"),
        "mqtt_queue_limit": 42,
    }

    module = types.SimpleNamespace(
        Uci=lambda: FakeCursor(payload),
        UciException=RuntimeError,
    )

    monkeypatch.setitem(sys.modules, "uci", module)

    config = utils.get_uci_config()

    assert config["serial_port"] == "uci-port"
    assert config["allowed_commands"] == "ls echo"
    assert config["mqtt_queue_limit"] == "42"


def test_get_uci_config_falls_back_on_errors(monkeypatch: pytest.MonkeyPatch):
    class FakeError(Exception):
        pass

    class FakeCursor:
        def __enter__(self) -> Self:
            return self

        def __exit__(self, exc_type, exc, _tb) -> bool:
            return False

        def get_all(self, package: str, section: str) -> dict[str, object]:
            raise FakeError("boom")

    module = types.SimpleNamespace(
        Uci=lambda: FakeCursor(),
        UciException=FakeError,
    )

    monkeypatch.setitem(sys.modules, "uci", module)

    fallback_called = False

    def fake_default() -> dict[str, str]:
        nonlocal fallback_called
        fallback_called = True
        return {"serial_port": "default"}

    monkeypatch.setattr(utils, "get_default_config", fake_default)

    config = utils.get_uci_config()

    assert fallback_called is True
    assert config == {"serial_port": "default"}
