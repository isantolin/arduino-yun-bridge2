"""Exhaustive tests for pin_rest_cgi, mcubridge_led_control, and mcubridge_hw_smoke scripts. [SIL-2]"""

from __future__ import annotations

from collections.abc import Callable
import importlib.util
import io
import json
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock

from dirty_equals import Contains, IsPartialDict
import pytest
from pytest_mock import MockerFixture

# Dynamically import pin_rest_cgi
script_path = Path(__file__).parent.parent / "scripts" / "pin_rest_cgi.py"
spec = importlib.util.spec_from_file_location("pin_rest_cgi", str(script_path))
if spec is None or spec.loader is None:
    raise ImportError("Could not load pin_rest_cgi.py")
pin_rest_cgi: Any = importlib.util.module_from_spec(spec)
spec.loader.exec_module(pin_rest_cgi)
application = pin_rest_cgi.application
extract_pin_from_path = pin_rest_cgi.extract_pin_from_path

# Dynamically import mcubridge_led_control and mcubridge_hw_smoke
_led_path = Path(__file__).parent.parent / "scripts" / "mcubridge_led_control.py"
_led_spec = importlib.util.spec_from_file_location("mcubridge_led_control", str(_led_path))
if _led_spec is None or _led_spec.loader is None:
    raise ImportError("Could not load mcubridge_led_control.py")
led_control: Any = importlib.util.module_from_spec(_led_spec)
_led_spec.loader.exec_module(led_control)

_smoke_path = Path(__file__).parent.parent / "scripts" / "mcubridge_hw_smoke.py"
_smoke_spec = importlib.util.spec_from_file_location("mcubridge_hw_smoke", str(_smoke_path))
if _smoke_spec is None or _smoke_spec.loader is None:
    raise ImportError("Could not load mcubridge_hw_smoke.py")
hw_smoke: Any = importlib.util.module_from_spec(_smoke_spec)
_smoke_spec.loader.exec_module(hw_smoke)


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


def test_extract_pin_from_path() -> None:
    """Validate declarative URI pin parser across supported route patterns."""
    assert extract_pin_from_path("/pin/13") == 13
    assert extract_pin_from_path("arduino/pin/5") == 5
    assert extract_pin_from_path("/digital/7") == 7
    assert extract_pin_from_path("/arduino/digital/9?foo=bar") == 9
    assert extract_pin_from_path("/invalid/path") is None
    assert extract_pin_from_path("/pin/notanumber") is None
    assert extract_pin_from_path("") is None


def test_cgi_success(cgi_env: Any, mocker: MockerFixture) -> None:
    env = cgi_env(body=json.dumps({"state": "ON"}).encode("utf-8"))
    start_response = mocker.MagicMock()
    mock_ubus = mocker.MagicMock()
    mock_ubus.connect.return_value = True
    mock_ubus.call.return_value = [{"status": "accepted"}]
    mocker.patch.object(pin_rest_cgi, "ubus", mock_ubus)

    res = application(env, start_response)
    assert start_response.called
    assert start_response.call_args[0][0] == Contains("200 OK")
    mock_ubus.call.assert_called_once_with("mcubridge", "digital_write", {"pin": 13, "value": 1})

    data = json.loads(res[0])
    assert data == IsPartialDict({"status": "accepted"})


def test_cgi_invalid_path(cgi_env: Any, mocker: MockerFixture) -> None:
    env = cgi_env(path="/invalid")
    start_response = mocker.MagicMock()
    application(env, start_response)
    assert start_response.call_args[0][0] == Contains("400 Bad Request")


def test_cgi_invalid_method(cgi_env: Any, mocker: MockerFixture) -> None:
    env = cgi_env(method="GET")
    start_response = mocker.MagicMock()
    application(env, start_response)
    assert start_response.call_args[0][0] == Contains("405 Method Not Allowed")


def test_cgi_invalid_state(cgi_env: Any, mocker: MockerFixture) -> None:
    env = cgi_env(body=json.dumps({"state": "INVALID"}).encode("utf-8"))
    start_response = mocker.MagicMock()
    application(env, start_response)
    assert start_response.call_args[0][0] == Contains("400 Bad Request")


def test_cgi_internal_error(cgi_env: Any, mocker: MockerFixture) -> None:
    env = cgi_env(body=json.dumps({"state": "ON"}).encode("utf-8"))
    start_response = mocker.MagicMock()
    mock_ubus = mocker.MagicMock()
    mock_ubus.connect.return_value = True
    mock_ubus.call.side_effect = OSError("fail")
    mocker.patch.object(pin_rest_cgi, "ubus", mock_ubus)

    application(env, start_response)
    assert start_response.call_args[0][0] == Contains("500 Internal Server Error")


def test_pin_rest_cgi_set_pin_digital_sync_error(mocker: MockerFixture) -> None:
    mocker.patch.object(pin_rest_cgi, "ubus", None)
    with pytest.raises(RuntimeError, match="Native OpenWrt UBUS module unavailable"):
        pin_rest_cgi.set_pin_digital_sync(13, 1)

    mock_ubus = mocker.MagicMock()
    mock_ubus.connect.return_value = True
    mock_ubus.call.side_effect = OSError("UBUS failure")
    mocker.patch.object(pin_rest_cgi, "ubus", mock_ubus)
    with pytest.raises(OSError, match="UBUS failure"):
        pin_rest_cgi.set_pin_digital_sync(13, 1)
    assert mock_ubus.connect.called
    mock_ubus.call.assert_called_once_with("mcubridge", "digital_write", {"pin": 13, "value": 1})

    mock_ubus_ok = mocker.MagicMock()
    mock_ubus_ok.connect.return_value = True
    mock_ubus_ok.call.return_value = [{"status": "accepted"}]
    mocker.patch.object(pin_rest_cgi, "ubus", mock_ubus_ok)
    pin_rest_cgi.set_pin_digital_sync(13, 1)
    assert mock_ubus_ok.connect.called
    mock_ubus_ok.call.assert_called_once_with("mcubridge", "digital_write", {"pin": 13, "value": 1})

    mock_ubus_rejected = mocker.MagicMock()
    mock_ubus_rejected.connect.return_value = True
    mock_ubus_rejected.call.return_value = [{"status": "error", "message": "Serial service unavailable"}]
    mocker.patch.object(pin_rest_cgi, "ubus", mock_ubus_rejected)
    with pytest.raises(RuntimeError, match="was not accepted"):
        pin_rest_cgi.set_pin_digital_sync(13, 1)


def test_pin_rest_cgi_application_branches(mocker: MockerFixture) -> None:
    start_response = mocker.MagicMock()
    body = b'{"state": "ON"}'
    env = {
        "PATH_INFO": "/pin/13",
        "REQUEST_METHOD": "POST",
        "CONTENT_LENGTH": str(len(body)),
        "wsgi.input": io.BytesIO(body),
    }

    mock_ubus = mocker.MagicMock()
    mock_ubus.connect.return_value = True
    mock_ubus.call.return_value = [{"status": "accepted"}]
    mocker.patch.object(pin_rest_cgi, "ubus", mock_ubus)
    res = pin_rest_cgi.application(env, start_response)
    assert res
    start_response.assert_called_once()
    mock_ubus.call.assert_called_once_with("mcubridge", "digital_write", {"pin": 13, "value": 1})

    start_response_err = mocker.MagicMock()
    env_invalid = {"PATH_INFO": "/invalid", "REQUEST_METHOD": "GET"}
    res_err = pin_rest_cgi.application(env_invalid, start_response_err)
    assert res_err
    assert start_response_err.call_args[0][0] == "400 Bad Request"
    assert start_response_err.call_args[0][1] == Contains(("Content-Type", "application/json"))


def test_mcubridge_led_control_ubus_contract() -> None:
    """Verify mcubridge_led_control uses direct python-ubus API and accepts 'accepted' status."""
    mock_ubus = MagicMock()
    mock_ubus.connect.return_value = True
    mock_ubus.call.return_value = [{"status": "accepted", "pin": 13, "value": 1}]

    assert led_control.set_digital_pin(13, 1, ubus_module=mock_ubus)
    mock_ubus.connect.assert_called_once_with()
    mock_ubus.call.assert_called_once_with("mcubridge", "digital_write", {"pin": 13, "value": 1})

    mock_ubus.call.return_value = [{"status": "error"}]
    assert not led_control.set_digital_pin(13, 0, ubus_module=mock_ubus)


def test_mcubridge_hw_smoke_ubus_contract() -> None:
    """Verify mcubridge_hw_smoke unwraps list[dict] from python-ubus module call()."""
    mock_ubus = MagicMock()
    mock_ubus.connect.return_value = True
    mock_ubus.call.return_value = [{"status": "ok", "connected": True, "synchronized": True}]

    res = hw_smoke.ubus_call("mcubridge", "ping", ubus_module=mock_ubus)
    assert res == {"status": "ok", "connected": True, "synchronized": True}
    mock_ubus.connect.assert_called_once_with()
    mock_ubus.call.assert_called_once_with("mcubridge", "ping", {})
