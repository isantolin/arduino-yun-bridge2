"""Exhaustive tests for pin_rest_cgi script. [SIL-2]"""

from __future__ import annotations

import importlib.util
import io
import json
import sys
import types
from pathlib import Path
from typing import Any, Callable
from unittest.mock import MagicMock

import pytest
from pytest_mock import MockerFixture

# Mock 'uci' before importing pin_rest_cgi
uci_mock = types.ModuleType("uci")
sys.modules["uci"] = uci_mock

# Dynamically import pin_rest_cgi
script_path = Path(__file__).parent.parent / "scripts" / "pin_rest_cgi.py"
spec = importlib.util.spec_from_file_location("pin_rest_cgi", str(script_path))
if spec is None or spec.loader is None:
    raise ImportError("Could not load pin_rest_cgi.py")
pin_rest_cgi = importlib.util.module_from_spec(spec)
sys.modules["pin_rest_cgi"] = pin_rest_cgi
spec.loader.exec_module(pin_rest_cgi)
application = pin_rest_cgi.application


@pytest.fixture
def cgi_env() -> Callable[..., dict[str, Any]]:
    def _make_env(path: str = "/pin/13", method: str = "POST", body: bytes | None = None) -> dict[str, Any]:
        env: dict[str, Any] = {
            "PATH_INFO": path,
            "REQUEST_METHOD": method,
            "CONTENT_LENGTH": str(len(body)) if body else "0",
            "wsgi.input": io.BytesIO(body or b""),
        }
        return env

    return _make_env


def test_cgi_success(cgi_env: Any, mocker: MockerFixture) -> None:
    env = cgi_env(body=json.dumps({"state": "ON"}).encode("utf-8"))
    start_response = MagicMock()

    mock_set_pin = mocker.patch("pin_rest_cgi.set_pin_digital_sync")
    mock_load = mocker.patch("pin_rest_cgi.load_runtime_config")
    mock_config = MagicMock()
    mock_config.topic_prefix = "br"
    mock_load.return_value = mock_config

    res = application(env, start_response)

    assert start_response.called
    assert "200 OK" in start_response.call_args[0][0]
    mock_set_pin.assert_called_once_with(13, 1)

    data = json.loads(
        res[0],
    )
    assert data["status"] == "ok"


def test_cgi_invalid_path(cgi_env: Any) -> None:
    env = cgi_env(path="/invalid")
    start_response = MagicMock()
    application(env, start_response)
    assert "400 Bad Request" in start_response.call_args[0][0]


def test_cgi_invalid_method(cgi_env: Any) -> None:
    env = cgi_env(method="GET")
    start_response = MagicMock()
    application(env, start_response)
    assert "405 Method Not Allowed" in start_response.call_args[0][0]


def test_cgi_invalid_state(cgi_env: Any) -> None:
    env = cgi_env(body=json.dumps({"state": "INVALID"}).encode("utf-8"))
    start_response = MagicMock()
    application(env, start_response)
    assert "400 Bad Request" in start_response.call_args[0][0]


def test_cgi_internal_error(cgi_env: Any, mocker: MockerFixture) -> None:
    env = cgi_env(body=json.dumps({"state": "ON"}).encode("utf-8"))
    start_response = MagicMock()
    mocker.patch("pin_rest_cgi.load_runtime_config", side_effect=OSError("fail"))
    application(env, start_response)
    assert "500 Internal Server Error" in start_response.call_args[0][0]


def test_pin_rest_cgi_set_pin_digital_sync_error(mocker: MockerFixture) -> None:
    mocker.patch.object(pin_rest_cgi, "ubus", None)
    with pytest.raises(RuntimeError, match="Native OpenWrt UBUS module unavailable"):
        pin_rest_cgi.set_pin_digital_sync(13, 1)

    mock_ubus = MagicMock()
    mock_ubus.call.side_effect = OSError("UBUS failure")
    mocker.patch.object(pin_rest_cgi, "ubus", mock_ubus)
    with pytest.raises(OSError, match="UBUS failure"):
        pin_rest_cgi.set_pin_digital_sync(13, 1)
    assert mock_ubus.connect.called
    mock_ubus.call.assert_called_once_with("mcubridge", "digital_write", {"pin": 13, "value": 1})

    mock_ubus_ok = MagicMock()
    mocker.patch.object(pin_rest_cgi, "ubus", mock_ubus_ok)
    pin_rest_cgi.set_pin_digital_sync(13, 1)
    assert mock_ubus_ok.connect.called
    mock_ubus_ok.call.assert_called_once_with("mcubridge", "digital_write", {"pin": 13, "value": 1})


def test_pin_rest_cgi_application_branches(mocker: MockerFixture) -> None:
    start_response = MagicMock()
    body = b'{"state": "ON"}'
    env = {
        "PATH_INFO": "/pin/13",
        "REQUEST_METHOD": "POST",
        "CONTENT_LENGTH": str(len(body)),
        "wsgi.input": io.BytesIO(body),
    }

    mock_set_pin = mocker.patch.object(pin_rest_cgi, "set_pin_digital_sync")
    res = pin_rest_cgi.application(env, start_response)
    assert res
    start_response.assert_called_once()
    mock_set_pin.assert_called_once_with(13, 1)

    start_response_err = MagicMock()
    env_invalid = {"PATH_INFO": "/invalid", "REQUEST_METHOD": "GET"}
    res_err = pin_rest_cgi.application(env_invalid, start_response_err)
    assert res_err
    start_response_err.assert_called_with(
        "400 Bad Request",
        [
            ("Content-Type", "application/json"),
            ("Content-Length", "52"),
            ("Access-Control-Allow-Origin", "*"),
            ("Access-Control-Allow-Methods", "GET, POST, OPTIONS"),
            ("Access-Control-Allow-Headers", "Content-Type"),
        ],
    )
