"""Unit tests for modernized pin_rest_cgi (SIL-2)."""

from __future__ import annotations

import importlib.util
from pathlib import Path
from types import ModuleType
from unittest.mock import MagicMock, patch

import pytest
from mcubridge.config.settings import RuntimeConfig


@pytest.fixture
def pin_rest_module() -> ModuleType:
    script_path = Path(__file__).parent.parent / "scripts" / "pin_rest_cgi.py"
    spec = importlib.util.spec_from_file_location("pin_rest_cgi", script_path)
    if spec is None or spec.loader is None:
        raise ImportError(f"Could not load {script_path}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_publish_sync_configures_tls(
    pin_rest_module: ModuleType,
    runtime_config: RuntimeConfig,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Patch paho.mqtt.publish.single
    with patch("paho.mqtt.publish.single") as mock_publish:
        runtime_config.mqtt_host = "localhost"
        runtime_config.mqtt_port = 1883
        runtime_config.mqtt_user = "user"
        runtime_config.mqtt_pass = "pass"
        runtime_config.mqtt_tls = True

        # Mock configure_tls_context using monkeypatch on the loaded module
        mock_ctx = MagicMock()
        monkeypatch.setattr(pin_rest_module, "configure_tls_context", lambda cfg: mock_ctx)

        pin_rest_module.publish_sync("topic", "1", runtime_config)

        mock_publish.assert_called_once()
        args, kwargs = mock_publish.call_args
        assert args[0] == "topic"
        assert kwargs["payload"] == "1"
        assert kwargs["hostname"] == "localhost"
        assert kwargs["port"] == 1883
        assert kwargs["auth"] == {"username": "user", "password": "pass"}
        assert kwargs["tls"] == {"context": mock_ctx}


def test_publish_sync_invokes_paho(
    pin_rest_module: ModuleType,
    runtime_config: RuntimeConfig,
) -> None:
    # Disable TLS in config to avoid cafile check in helper
    runtime_config.mqtt_tls = False
    with patch("paho.mqtt.publish.single") as mock_publish:
        pin_rest_module.publish_sync("topic/test", "0", runtime_config)
        mock_publish.assert_called_once_with(
            "topic/test",
            payload="0",
            qos=1,
            hostname=runtime_config.mqtt_host,
            port=runtime_config.mqtt_port,
            auth=None,
            tls=None,
        )


def test_application_invokes_publish(
    pin_rest_module: ModuleType,
    runtime_config: RuntimeConfig,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """End-to-end WSGI application test."""

    # Mock dependencies
    monkeypatch.setattr(pin_rest_module, "load_runtime_config", lambda: runtime_config)
    monkeypatch.setattr(pin_rest_module, "publish_sync", MagicMock())

    # Ensure no logging errors interfere
    monkeypatch.setattr(pin_rest_module, "configure_logging", MagicMock())

    def start_response(status: str, headers: list[tuple[str, str]]) -> None:
        assert "200 OK" in status

    import io

    environ = {
        "PATH_INFO": "/pin/13",
        "REQUEST_METHOD": "POST",
        "CONTENT_LENGTH": str(len(b'{"state": "ON"}')),
        "wsgi.input": io.BytesIO(b'{"state": "ON"}'),
    }

    res = pin_rest_module.application(environ, start_response)
    assert b"ok" in res[0]
    pin_rest_module.publish_sync.assert_called_once()
