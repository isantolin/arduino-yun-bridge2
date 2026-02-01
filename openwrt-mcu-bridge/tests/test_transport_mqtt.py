"""Unit tests for mcubridge.transport.mqtt."""

from __future__ import annotations

import asyncio
from pathlib import Path
from types import SimpleNamespace
import pytest

import aiomqtt

from mcubridge.config.settings import RuntimeConfig
from mcubridge.const import (
    DEFAULT_CONSOLE_QUEUE_LIMIT_BYTES,
    DEFAULT_MAILBOX_QUEUE_BYTES_LIMIT,
    DEFAULT_MAILBOX_QUEUE_LIMIT,
    DEFAULT_MQTT_PORT,
    DEFAULT_PROCESS_TIMEOUT,
    DEFAULT_RECONNECT_DELAY,
    DEFAULT_STATUS_INTERVAL,
)
from mcubridge.mqtt.messages import QueuedPublish
from mcubridge.rpc import protocol
from mcubridge.services.runtime import BridgeService
from mcubridge.state.context import create_runtime_state
from mcubridge.transport import mqtt


def _make_config(*, tls: bool, cafile: str | None) -> RuntimeConfig:
    return RuntimeConfig(
        serial_port="/dev/null",
        serial_baud=protocol.DEFAULT_BAUDRATE,
        serial_safe_baud=protocol.DEFAULT_SAFE_BAUDRATE,
        mqtt_host="localhost",
        mqtt_port=DEFAULT_MQTT_PORT,
        mqtt_user=None,
        mqtt_pass=None,
        mqtt_tls=tls,
        mqtt_cafile=cafile,
        mqtt_certfile=None,
        mqtt_keyfile=None,
        mqtt_topic=protocol.MQTT_DEFAULT_TOPIC_PREFIX,
        allowed_commands=(),
        file_system_root="/tmp",
        process_timeout=DEFAULT_PROCESS_TIMEOUT,
        reconnect_delay=DEFAULT_RECONNECT_DELAY,
        status_interval=DEFAULT_STATUS_INTERVAL,
        debug_logging=False,
        console_queue_limit_bytes=DEFAULT_CONSOLE_QUEUE_LIMIT_BYTES,
        mailbox_queue_limit=DEFAULT_MAILBOX_QUEUE_LIMIT,
        mailbox_queue_bytes_limit=DEFAULT_MAILBOX_QUEUE_BYTES_LIMIT,
        serial_shared_secret=b"testshared",
    )


def test_configure_tls_disabled_returns_none(tmp_path: Path) -> None:
    config = _make_config(tls=False, cafile=str(tmp_path / "ca.pem"))
    assert mqtt._configure_tls(config) is None


def test_configure_tls_missing_cafile_raises(tmp_path: Path) -> None:
    config = _make_config(tls=True, cafile=str(tmp_path / "missing.pem"))
    with pytest.raises(RuntimeError, match="MQTT TLS CA file missing"):
        mqtt._configure_tls(config)


def test_configure_tls_loads_cert_chain_when_provided(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    cafile = tmp_path / "ca.pem"
    cafile.write_text("not-a-real-ca")

    calls: list[tuple[str, str]] = []

    def _load_cert_chain(certfile: str, keyfile: str) -> None:
        calls.append((certfile, keyfile))

    fake_context = SimpleNamespace(
        minimum_version=None,
        load_cert_chain=_load_cert_chain,
    )

    def _fake_create_default_context(*_args, **_kwargs):
        return fake_context

    monkeypatch.setattr(
        mqtt.ssl,
        "create_default_context",
        _fake_create_default_context,
    )

    config = _make_config(tls=True, cafile=str(cafile))
    config.mqtt_certfile = str(tmp_path / "client.pem")
    config.mqtt_keyfile = str(tmp_path / "client.key")

    ctx = mqtt._configure_tls(config)
    assert ctx is fake_context
    assert calls == [(config.mqtt_certfile, config.mqtt_keyfile)]


def test_configure_tls_wraps_ssl_errors(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    cafile = tmp_path / "ca.pem"
    cafile.write_text("not-a-real-ca")

    def _boom(*_args, **_kwargs):
        raise ValueError("bad")

    monkeypatch.setattr(mqtt.ssl, "create_default_context", _boom)

    config = _make_config(tls=True, cafile=str(cafile))
    with pytest.raises(RuntimeError, match=r"TLS setup failed"):
        mqtt._configure_tls(config)


@pytest.mark.asyncio
async def test_mqtt_task_requeues_on_publish_failure(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    config = _make_config(tls=False, cafile=None)
    state = create_runtime_state(config)
    service = BridgeService(config, state)

    # Seed one outgoing message.
    await state.mqtt_publish_queue.put(
        QueuedPublish(
            topic_name=f"{protocol.MQTT_DEFAULT_TOPIC_PREFIX}/test/topic",
            payload=b"hello",
        )
    )

    created: list[object] = []

    class FakeClient:
        def __init__(self, **_kwargs):
            self.subscribed: list[tuple[str, int]] = []
            created.append(self)

        async def __aenter__(self):
            return self

        async def __aexit__(self, exc_type, exc, _tb):
            return False

        async def subscribe(self, topic: str, qos: int = 0):
            self.subscribed.append((topic, qos))

        async def publish(self, *_args, **_kwargs):
            raise aiomqtt.MqttError("publish failed")

        @property
        def messages(self):
            async def _iter():
                # Will be cancelled when publisher fails.
                # Use short sleep to avoid CI timeout if cancellation is delayed.
                while True:
                    await asyncio.sleep(0.01)
                    yield None

            return _iter()

    monkeypatch.setattr(mqtt.aiomqtt, "Client", FakeClient)

    # Stop after the first reconnect attempt.
    async def _cancel_sleep(_seconds: float):
        raise asyncio.CancelledError

    monkeypatch.setattr(mqtt.asyncio, "sleep", _cancel_sleep)

    with pytest.raises(asyncio.CancelledError):
        await mqtt.mqtt_task(config, state, service)

    # Publisher requeues the message on publish error.
    assert state.mqtt_publish_queue.qsize() >= 1

    # Subscriptions are configured.
    assert created, "Expected mqtt_task to instantiate a client"
    client = created[0]
    assert len(getattr(client, "subscribed")) >= 10
