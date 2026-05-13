import sys
from unittest.mock import MagicMock

sys.modules["uci"] = MagicMock()

import msgspec
import pytest
from unittest.mock import patch
from io import BytesIO
from pathlib import Path
import importlib.util
import sys

# Dynamically import pin_rest_cgi
script_path = Path(__file__).parent.parent / "scripts" / "pin_rest_cgi.py"
spec = importlib.util.spec_from_file_location("pin_rest_cgi", str(script_path))
if spec is None or spec.loader is None:
    raise ImportError("Could not load pin_rest_cgi.py")
pin_rest_cgi = importlib.util.module_from_spec(spec)
sys.modules["pin_rest_cgi"] = pin_rest_cgi
spec.loader.exec_module(pin_rest_cgi)
application = getattr(pin_rest_cgi, "application")

from mcubridge.protocol.structures import GenericResponsePacket


@pytest.fixture
def cgi_env():
    def _make_env(path="/pin/13", method="POST", body=None):
        env = {
            "PATH_INFO": path,
            "REQUEST_METHOD": method,
            "wsgi.input": BytesIO(body) if body else BytesIO(),
            "CONTENT_LENGTH": str(len(body)) if body else "0",
        }
        return env

    return _make_env


def test_cgi_success(cgi_env):
    env = cgi_env(body=msgspec.json.encode({"state": "ON"}))
    start_response = MagicMock()

    with patch("paho.mqtt.publish.single") as mock_publish:
        with patch("pin_rest_cgi.load_runtime_config") as mock_load:
            mock_config = MagicMock()
            mock_config.mqtt_topic = "br"
            mock_config.get_ssl_context.return_value = None
            mock_config.mqtt_user = None
            mock_load.return_value = mock_config

            res = application(env, start_response)

            assert start_response.called
            assert "200 OK" in start_response.call_args[0][0]
            mock_publish.assert_called()

            data = msgspec.json.decode(res[0], type=GenericResponsePacket)
            assert data.status == "ok"


def test_cgi_invalid_path(cgi_env):
    env = cgi_env(path="/invalid")
    start_response = MagicMock()
    application(env, start_response)
    assert "400 Bad Request" in start_response.call_args[0][0]


def test_cgi_invalid_method(cgi_env):
    env = cgi_env(method="GET")
    start_response = MagicMock()
    application(env, start_response)
    assert "405 Method Not Allowed" in start_response.call_args[0][0]


def test_cgi_invalid_state(cgi_env):
    env = cgi_env(body=msgspec.json.encode({"state": "INVALID"}))
    start_response = MagicMock()
    application(env, start_response)
    assert "400 Bad Request" in start_response.call_args[0][0]


def test_cgi_internal_error(cgi_env):
    env = cgi_env(body=msgspec.json.encode({"state": "ON"}))
    start_response = MagicMock()
    with patch("pin_rest_cgi.load_runtime_config", side_effect=OSError("fail")):
        application(env, start_response)
        assert "500 Internal Server Error" in start_response.call_args[0][0]
