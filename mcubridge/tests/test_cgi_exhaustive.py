import importlib.util
import sys
import types
from io import BytesIO
from pathlib import Path
from typing import Any
from collections.abc import Callable
from unittest.mock import MagicMock, patch

import json
import pytest

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
application = getattr(pin_rest_cgi, "application")


@pytest.fixture
def cgi_env() -> Callable[..., dict[str, Any]]:
    def _make_env(path: str = "/pin/13", method: str = "POST", body: bytes | None = None) -> dict[str, Any]:
        env: dict[str, Any] = {
            "PATH_INFO": path,
            "REQUEST_METHOD": method,
            "wsgi.input": BytesIO(body) if body else BytesIO(),
            "CONTENT_LENGTH": str(len(body)) if body else "0",
        }
        return env

    return _make_env


def test_cgi_success(cgi_env: Any) -> None:
    env = cgi_env(body=json.dumps({"state": "ON"}).encode("utf-8"))
    start_response = MagicMock()

    with patch("pin_rest_cgi.publish_sync") as mock_publish:
        with patch("pin_rest_cgi.load_runtime_config") as mock_load:
            mock_config = MagicMock()
            mock_config.topic_prefix = "br"
            mock_config.get_ssl_context.return_value = None
            mock_config.mqtt_user = None
            mock_load.return_value = mock_config

            res = application(env, start_response)

            assert start_response.called
            assert "200 OK" in start_response.call_args[0][0]
            mock_publish.assert_called_once_with("br/d/13", "1", mock_config)

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


def test_cgi_internal_error(cgi_env: Any) -> None:
    env = cgi_env(body=json.dumps({"state": "ON"}).encode("utf-8"))
    start_response = MagicMock()
    with patch("pin_rest_cgi.load_runtime_config", side_effect=OSError("fail")):
        application(env, start_response)
        assert "500 Internal Server Error" in start_response.call_args[0][0]
