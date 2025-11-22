"""Regression tests for the pin_rest_cgi MQTT helper."""
from __future__ import annotations

import importlib.util
import io
import json
import sys
from importlib.abc import Loader
from pathlib import Path
from types import ModuleType, SimpleNamespace
from typing import Any

import pytest

from yunbridge.config.settings import RuntimeConfig


def _load_pin_rest_cgi() -> ModuleType:
    script_path = (
        Path(__file__).resolve().parents[2]
        / "openwrt-yun-core"
        / "scripts"
        / "pin_rest_cgi.py"
    )
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


class _FakeResult:
    def __init__(self, published: bool = True) -> None:
        self._published = published

    def is_published(self) -> bool:  # pragma: no cover - simple accessor
        return self._published


class _FakeClient:
    def __init__(self, *, should_timeout: bool = False) -> None:
        self.should_timeout = should_timeout
        self.protocol: int | None = None
        self.username_credentials: tuple[str | None, str | None] | None = None
        self.tls_arguments: dict[str, Any] | None = None
        self.tls_insecure: bool | None = None
        self.logger = None
        self.connection: tuple[str, int, int] | None = None
        self.loop_started = False
        self.loop_stopped = False
        self.disconnected = False
        self.publish_arguments: tuple[str, str, int, bool] | None = None

    def username_pw_set(
        self,
        username: str | None,
        password: str | None,
    ) -> None:
        self.username_credentials = (username, password)

    def tls_set(
        self,
        *,
        ca_certs: str,
        certfile: str | None,
        keyfile: str | None,
        tls_version: Any,
    ) -> None:
        self.tls_arguments = {
            "ca_certs": ca_certs,
            "certfile": certfile,
            "keyfile": keyfile,
            "tls_version": tls_version,
        }

    def tls_insecure_set(self, allow: bool) -> None:
        self.tls_insecure = allow

    def enable_logger(self, logger: Any) -> None:
        self.logger = logger

    def connect(self, host: str, port: int, keepalive: int) -> None:
        self.connection = (host, port, keepalive)

    def loop_start(self) -> None:
        self.loop_started = True

    def publish(
        self,
        topic: str,
        payload: str,
        qos: int,
        retain: bool,
    ) -> _FakeResult:
        self.publish_arguments = (topic, payload, qos, retain)
        return _FakeResult(not self.should_timeout)

    def loop_stop(self) -> None:
        self.loop_stopped = True

    def disconnect(self) -> None:
        self.disconnected = True


@pytest.fixture()
def pin_rest_module() -> ModuleType:
    return _load_pin_rest_cgi()


def test_publish_with_retries_configures_tls(
    pin_rest_module: ModuleType,
    runtime_config: RuntimeConfig,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fake_client = _FakeClient()

    monkeypatch.setattr(
        pin_rest_module,
        "mqtt",
        SimpleNamespace(Client=lambda *_, **__: fake_client),
    )

    runtime_config.mqtt_user = "user"
    runtime_config.mqtt_pass = "secret"

    pin_rest_module.publish_with_retries(
        topic="br/d/13",
        payload="1",
        config=runtime_config,
        retries=1,
        publish_timeout=0.1,
    )

    assert fake_client.username_credentials == ("user", "secret")
    assert fake_client.tls_arguments is not None
    assert fake_client.tls_arguments["ca_certs"] == runtime_config.mqtt_cafile
    assert fake_client.tls_insecure is False
    assert fake_client.connection == (
        runtime_config.mqtt_host,
        runtime_config.mqtt_port,
        60,
    )
    assert fake_client.publish_arguments == ("br/d/13", "1", 1, False)
    assert fake_client.loop_started is True
    assert fake_client.loop_stopped is True
    assert fake_client.disconnected is True


def test_publish_with_retries_times_out(
    pin_rest_module: ModuleType,
    runtime_config: RuntimeConfig,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fake_client = _FakeClient(should_timeout=True)
    monkeypatch.setattr(
        pin_rest_module,
        "mqtt",
        SimpleNamespace(Client=lambda *_, **__: fake_client),
    )

    with pytest.raises(TimeoutError):
        pin_rest_module.publish_with_retries(
            topic="br/d/2",
            payload="0",
            config=runtime_config,
            retries=1,
            publish_timeout=0.0,
        )


def test_main_successful_publication(monkeypatch: pytest.MonkeyPatch) -> None:
    module = _load_pin_rest_cgi()

    fake_config = SimpleNamespace(
        mqtt_topic="br",
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

    def _fake_publish(topic: str, payload: str, config: Any, **_: Any) -> None:
        captured["topic"] = topic
        captured["payload"] = payload
        captured["config"] = config

    monkeypatch.setattr(module, "load_runtime_config", lambda: fake_config)
    monkeypatch.setattr(module, "publish_with_retries", _fake_publish)

    environ: dict[str, str] = {
        "REQUEST_METHOD": "POST",
        "PATH_INFO": "/pin/7",
        "CONTENT_LENGTH": "17",
    }
    monkeypatch.setattr(module.os, "environ", environ)
    monkeypatch.setattr(
        module.sys,
        "stdin",
        io.StringIO(json.dumps({"state": "ON"})),
    )
    output = io.StringIO()
    monkeypatch.setattr(module.sys, "stdout", output)

    module.main()

    body = output.getvalue().split("\n\n", 1)[1]
    response = json.loads(body)
    assert response["status"] == "ok"
    assert captured["topic"] == "br/d/7"
    assert captured["payload"] == "1"


def test_main_rejects_invalid_state(monkeypatch: pytest.MonkeyPatch) -> None:
    module = _load_pin_rest_cgi()

    fake_config = SimpleNamespace(
        mqtt_topic="br",
        mqtt_host="localhost",
        mqtt_port=1883,
        mqtt_user=None,
        mqtt_pass=None,
        mqtt_tls=True,
        mqtt_cafile="/tmp/test-ca.pem",
        mqtt_certfile=None,
        mqtt_keyfile=None,
    )

    monkeypatch.setattr(module, "load_runtime_config", lambda: fake_config)
    monkeypatch.setattr(
        module.os,
        "environ",
        {
            "REQUEST_METHOD": "POST",
            "PATH_INFO": "/pin/9",
            "CONTENT_LENGTH": "15",
        },
    )
    monkeypatch.setattr(
        module.sys,
        "stdin",
        io.StringIO(json.dumps({"state": "MAYBE"})),
    )
    output = io.StringIO()
    monkeypatch.setattr(module.sys, "stdout", output)

    module.main()

    body = output.getvalue().split("\n\n", 1)[1]
    response = json.loads(body)
    assert response["status"] == "error"
    assert "State must" in response["message"]
