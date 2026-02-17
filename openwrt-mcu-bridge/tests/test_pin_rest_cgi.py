"""Regression tests for the pin_rest_cgi MQTT helper."""

from __future__ import annotations

import importlib.util
import io
import os
import sys
from importlib.abc import Loader
from pathlib import Path
from types import ModuleType, SimpleNamespace
from typing import Any

import msgspec
import pytest
from mcubridge.config.settings import RuntimeConfig
from mcubridge.protocol import protocol


def _load_pin_rest_cgi() -> ModuleType:
    script_path = Path(__file__).resolve().parents[2] / "openwrt-mcu-core" / "scripts" / "pin_rest_cgi.py"
    spec = importlib.util.spec_from_file_location("pin_rest_cgi", script_path)
    if spec is None or spec.loader is None:
        raise RuntimeError("Unable to load pin_rest_cgi script")
    module = importlib.util.module_from_spec(spec)
    loader = spec.loader
    if not isinstance(loader, Loader):
        raise RuntimeError("pin_rest_cgi loader is not compatible")
    sys.modules[spec.name] = module
    loader.exec_module(module)
    return module

@pytest.fixture()
def pin_rest_module() -> ModuleType:
    return _load_pin_rest_cgi()


class MockInfo:
    def __init__(self, published: bool = True):
        self._published = published
    def is_published(self) -> bool:
        return self._published


class CapturingFakeClient:
    def __init__(self, *args: Any, **kwargs: Any) -> None:
        self.args = args
        self.kwargs = kwargs
        self.tls_kwargs: dict[str, Any] = {}
        self.auth_args: tuple[Any, ...] = ()
        self.published: list[tuple[str, str | bytes, int]] = []

    def tls_set(self, **kwargs: Any) -> None:
        self.tls_kwargs = kwargs

    def username_pw_set(self, *args: Any) -> None:
        self.auth_args = args

    def connect(self, *args: Any, **kwargs: Any) -> None:
        pass

    def loop_start(self) -> None:
        pass

    def loop_stop(self) -> None:
        pass

    def disconnect(self) -> None:
        pass

    def publish(self, topic: str, payload: str | bytes, qos: int = 0) -> Any:
        self.published.append((topic, payload, qos))
        return MockInfo()


def test_publish_safe_configures_tls(
    pin_rest_module: ModuleType,
    runtime_config: RuntimeConfig,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    captured_clients: list[CapturingFakeClient] = []

    class TestClient(CapturingFakeClient):
        def __init__(self, *args: Any, **kwargs: Any) -> None:
            super().__init__(*args, **kwargs)
            captured_clients.append(self)

    monkeypatch.setattr(pin_rest_module, "Client", TestClient)

    import ssl
    monkeypatch.setattr(ssl, "create_default_context", lambda **kwargs: "FAKE_TLS_CONTEXT")

    runtime_config.mqtt_user = "user"
    runtime_config.mqtt_pass = "secret"
    runtime_config.mqtt_tls = True
    cafile = tmp_path / "test-ca.pem"
    cafile.write_text("dummy-ca")
    runtime_config.mqtt_cafile = str(cafile)

    pin_rest_module.publish_safe(
        topic=f"{protocol.MQTT_DEFAULT_TOPIC_PREFIX}/d/13",
        payload="1",
        config=runtime_config
    )

    assert len(captured_clients) == 1
    fake_client = captured_clients[0]
    assert fake_client.auth_args == ("user", "secret")
    assert fake_client.tls_kwargs["ca_certs"] == str(cafile)


def test_publish_safe_times_out(
    pin_rest_module: ModuleType,
    runtime_config: RuntimeConfig,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class TimeoutClient(CapturingFakeClient):
        def publish(self, topic: str, payload: str | bytes, qos: int = 0) -> Any:
            return MockInfo(published=False)

    monkeypatch.setattr(pin_rest_module, "Client", TimeoutClient)
    # Patch retry to fail fast
    monkeypatch.setattr(pin_rest_module, "DEFAULT_RETRIES", 1)
    monkeypatch.setattr(pin_rest_module, "DEFAULT_PUBLISH_TIMEOUT", 0.01)

    with pytest.raises((TimeoutError, Exception)):
        pin_rest_module.publish_safe(
            topic="br/d/2",
            payload="0",
            config=runtime_config
        )


def test_main_invokes_publish(
    pin_rest_module: ModuleType,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fake_config = SimpleNamespace(
        mqtt_topic=protocol.MQTT_DEFAULT_TOPIC_PREFIX,
        mqtt_host="localhost",
        mqtt_port=1883,
        mqtt_user=None,
        mqtt_pass=None,
        mqtt_tls=True,
        mqtt_cafile="/tmp/test-ca.pem",
        mqtt_certfile=None,
        mqtt_keyfile=None,
    )

    captured: dict[str, Any] = {}
    def _fake_publish(topic: str, payload: str, config: Any) -> None:
        captured["topic"] = topic
        captured["payload"] = payload

    monkeypatch.setattr(pin_rest_module, "load_runtime_config", lambda: fake_config)
    monkeypatch.setattr(pin_rest_module, "publish_safe", _fake_publish)
    monkeypatch.setattr(pin_rest_module, "configure_logging", lambda config: None)

    environ = {
        "REQUEST_METHOD": "POST",
        "PATH_INFO": "/pin/7",
        "CONTENT_LENGTH": str(len(msgspec.json.encode({"state": "ON"}))),
        "wsgi.input": io.BytesIO(msgspec.json.encode({"state": "ON"}))
    }
    monkeypatch.setattr(os, "environ", environ)

    def start_response(status, headers):
        captured["status"] = status

    result = pin_rest_module.application(environ, start_response)
    body = msgspec.json.decode(b"".join(result))

    assert captured["topic"] == "br/d/7"
    assert captured["payload"] == "1"
    assert body["status"] == "ok"
    assert captured["status"] == "200 OK"


def test_main_rejects_invalid_state(
    pin_rest_module: ModuleType,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fake_config = SimpleNamespace(
        mqtt_topic=protocol.MQTT_DEFAULT_TOPIC_PREFIX,
        mqtt_host="localhost",
        mqtt_port=1883,
        mqtt_user=None,
        mqtt_pass=None,
        mqtt_tls=True,
        mqtt_cafile="/tmp/test-ca.pem",
        mqtt_certfile=None,
        mqtt_keyfile=None,
    )

    monkeypatch.setattr(pin_rest_module, "load_runtime_config", lambda: fake_config)
    monkeypatch.setattr(pin_rest_module, "configure_logging", lambda config: None)

    environ = {
        "REQUEST_METHOD": "POST",
        "PATH_INFO": "/pin/9",
        "CONTENT_LENGTH": str(len(msgspec.json.encode({"state": "MAYBE"}))),
        "wsgi.input": io.BytesIO(msgspec.json.encode({"state": "MAYBE"}))
    }
    monkeypatch.setattr(os, "environ", environ)

    captured_status = []
    def start_response(status, headers):
        captured_status.append(status)

    result = pin_rest_module.application(environ, start_response)
    body = msgspec.json.decode(b"".join(result))

    assert captured_status[0] == "400 Bad Request"
    assert body["status"] == "error"
    assert "Invalid state" in body["message"]
