"""Tests for the Pin REST CGI helper."""

from __future__ import annotations

import io
from types import ModuleType
from typing import Any
from unittest.mock import MagicMock

import msgspec
import pytest
from mcubridge.config.settings import RuntimeConfig
from mcubridge.protocol import protocol


@pytest.fixture
def pin_rest_module() -> ModuleType:
    import importlib.util
    from pathlib import Path

    script_path = Path(__file__).parent.parent / "scripts" / "pin_rest_cgi.py"
    spec = importlib.util.spec_from_file_location("pin_rest_cgi", script_path)
    if spec is None or spec.loader is None:
        raise ImportError(f"Could not load script from {script_path}")

    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


class MockInfo:
    def __init__(self, published: bool = True) -> None:
        self._published = published
        self.rc = 0

    def wait_for_publish(self, timeout: float | None = None) -> None:
        if not self._published:
            raise TimeoutError("Mock timeout")

    def is_published(self) -> bool:
        return self._published


class CapturingFakeClient:
    def __init__(self, *args: Any, **kwargs: Any) -> None:
        self.connected = False
        self.published_topic: str | None = None
        self.published_payload: str | bytes | None = None
        self.tls_context: Any = None

    def connect(self, host: str, port: int, keepalive: int = 60) -> None:
        self.connected = True

    def tls_set_context(self, context: Any) -> None:
        self.tls_context = context

    def username_pw_set(self, user: str, password: str | None = None) -> None:
        pass

    def publish(self, topic: str, payload: str | bytes, qos: int = 0) -> Any:
        self.published_topic = topic
        self.published_payload = payload
        return MockInfo()

    def disconnect(self) -> None:
        self.connected = False

    def loop_start(self) -> None:
        pass

    def loop_stop(self) -> None:
        pass


def test_publish_sync_configures_tls(
    pin_rest_module: ModuleType,
    runtime_config: RuntimeConfig,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Any,
) -> None:
    captured_clients: list[CapturingFakeClient] = []

    class TestClient(CapturingFakeClient):
        def __init__(self, *args: Any, **kwargs: Any) -> None:
            super().__init__(*args, **kwargs)
            captured_clients.append(self)

    monkeypatch.setattr(pin_rest_module, "Client", TestClient)

    import ssl
    monkeypatch.setattr(
        ssl,
        "create_default_context",
        lambda *args, **kwargs: MagicMock(),  # type: ignore[reportUnknownLambdaType]
    )

    runtime_config.mqtt_user = "user"
    runtime_config.mqtt_pass = "secret"
    runtime_config.mqtt_tls = True
    cafile = tmp_path / "test-ca.pem"
    cafile.write_text("dummy-ca")
    runtime_config.mqtt_cafile = str(cafile)

    pin_rest_module.publish_sync(
        topic=f"{protocol.MQTT_DEFAULT_TOPIC_PREFIX}/d/13",
        payload="1",
        config=runtime_config,
    )

    assert len(captured_clients) == 1
    assert captured_clients[0].connected is False  # Disconnected at end
    assert captured_clients[0].published_topic == f"{protocol.MQTT_DEFAULT_TOPIC_PREFIX}/d/13"


def test_publish_sync_times_out(
    pin_rest_module: ModuleType,
    runtime_config: RuntimeConfig,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class TimeoutClient(CapturingFakeClient):
        def publish(self, topic: str, payload: str | bytes, qos: int = 0) -> Any:
            return MockInfo(published=False)

    monkeypatch.setattr(pin_rest_module, "Client", TimeoutClient)
    runtime_config.mqtt_tls = False

    with pytest.raises(TimeoutError):
        pin_rest_module.publish_sync(topic="br/d/2", payload="0", config=runtime_config)


def test_application_invokes_publish(
    pin_rest_module: ModuleType,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from types import SimpleNamespace
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
        mqtt_tls_insecure=False,
        tls_enabled=True,
    )

    captured: dict[str, Any] = {}

    monkeypatch.setattr(pin_rest_module, "load_runtime_config", lambda: fake_config)
    monkeypatch.setattr(
        pin_rest_module,
        "publish_sync",
        lambda topic, payload, config: captured.update(  # type: ignore[reportUnknownLambdaType]
            {"topic": topic, "payload": payload}
        ),
    )
    monkeypatch.setattr(
        pin_rest_module,
        "configure_logging",
        lambda config: None,  # type: ignore[reportUnknownLambdaType]
    )

    environ = {
        "REQUEST_METHOD": "POST",
        "PATH_INFO": "/pin/7",
        "CONTENT_LENGTH": str(len(msgspec.json.encode({"state": "ON"}))),
        "wsgi.input": io.BytesIO(msgspec.json.encode({"state": "ON"})),
    }

    start_response = MagicMock()
    response = pin_rest_module.application(environ, start_response)

    assert captured["topic"] == f"{protocol.MQTT_DEFAULT_TOPIC_PREFIX}/d/7"
    assert captured["payload"] == "1"
    assert b"\"status\":\"ok\"" in response[0]
    start_response.assert_called_once()
