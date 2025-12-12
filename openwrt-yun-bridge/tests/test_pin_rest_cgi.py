"""Regression tests for the pin_rest_cgi MQTT helper."""

from __future__ import annotations

import asyncio
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


@pytest.fixture()
def pin_rest_module() -> ModuleType:
    return _load_pin_rest_cgi()


class _FakeAsyncClient:
    def __init__(self, *args: Any, **kwargs: Any) -> None:
        self.args = args
        self.kwargs = kwargs
        self.published: list[tuple[str, str | bytes, int, bool]] = []
        self.should_timeout = kwargs.get("timeout") == 0.001  # Hack for timeout test

    async def __aenter__(self) -> _FakeAsyncClient:
        if self.should_timeout:
            raise asyncio.TimeoutError()
        return self

    async def __aexit__(self, exc_type: Any, exc: Any, tb: Any) -> None:
        pass

    async def publish(
        self,
        topic: str,
        payload: str | bytes,
        qos: int = 0,
        retain: bool = False,
        properties: Any = None,
    ) -> None:
        self.published.append((topic, payload, qos, retain))


def test_publish_with_retries_configures_tls(
    pin_rest_module: ModuleType,
    runtime_config: RuntimeConfig,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured_clients = []

    class CapturingFakeClient(_FakeAsyncClient):
        def __init__(self, *args: Any, **kwargs: Any) -> None:
            super().__init__(*args, **kwargs)
            captured_clients.append(self)

    # Mock aiomqtt.Client
    import aiomqtt

    monkeypatch.setattr(aiomqtt, "Client", CapturingFakeClient)

    # Mock ssl to avoid file not found errors
    import ssl

    monkeypatch.setattr(
        ssl, "create_default_context", lambda **kwargs: "FAKE_TLS_CONTEXT"
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

    assert len(captured_clients) == 1
    fake_client = captured_clients[0]

    assert fake_client.kwargs["username"] == "user"
    assert fake_client.kwargs["password"] == "secret"
    assert fake_client.kwargs["tls_context"] == "FAKE_TLS_CONTEXT"
    assert fake_client.kwargs["hostname"] == runtime_config.mqtt_host
    assert fake_client.kwargs["port"] == runtime_config.mqtt_port

    assert len(fake_client.published) == 1
    assert fake_client.published[0] == ("br/d/13", b"1", 1, False)


def test_publish_with_retries_times_out(
    pin_rest_module: ModuleType,
    runtime_config: RuntimeConfig,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    # Mock aiomqtt.Client to raise TimeoutError
    import yunbridge.mqtt.publisher

    class TimeoutClient:
        def __init__(self, *args, **kwargs):
            pass

        async def __aenter__(self):
            raise asyncio.TimeoutError()

        async def __aexit__(self, *args):
            pass

    monkeypatch.setattr(
        yunbridge.mqtt.publisher.aiomqtt,
        "Client",
        TimeoutClient,
    )

    pin_rest_module.publish_with_retries(
        topic="br/d/2",
        payload="0",
        config=runtime_config,
        retries=1,
        publish_timeout=0.0,
    )

    assert "Failed to publish message after retries" in caplog.text


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
