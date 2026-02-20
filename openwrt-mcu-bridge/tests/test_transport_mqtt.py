"""Unit tests for mcubridge.transport.mqtt."""

from __future__ import annotations

import asyncio
import ssl
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import aiomqtt
import pytest
from mcubridge.config.const import (
    DEFAULT_CONSOLE_QUEUE_LIMIT_BYTES,
    DEFAULT_MAILBOX_QUEUE_BYTES_LIMIT,
    DEFAULT_MAILBOX_QUEUE_LIMIT,
    DEFAULT_MQTT_PORT,
    DEFAULT_PROCESS_TIMEOUT,
    DEFAULT_RECONNECT_DELAY,
    DEFAULT_STATUS_INTERVAL,
)
from mcubridge.config.settings import RuntimeConfig
from mcubridge.mqtt.messages import QueuedPublish
from mcubridge.protocol import protocol, structures
from mcubridge.services.runtime import BridgeService
from mcubridge.state.context import create_runtime_state
from mcubridge.transport import mqtt
from mcubridge.util import mqtt_helper


def _make_config(*, tls: bool, cafile: str | None) -> RuntimeConfig:
    return RuntimeConfig(
        serial_port="/dev/null",
        serial_baud=protocol.DEFAULT_BAUDRATE,
        serial_safe_baud=protocol.DEFAULT_SAFE_BAUDRATE,
        mqtt_host="localhost",
        mqtt_port=DEFAULT_MQTT_PORT,
        mqtt_user="user",
        mqtt_pass="pass",
        mqtt_tls=tls,
        mqtt_cafile=cafile,
        mqtt_certfile=None,
        mqtt_keyfile=None,
        mqtt_topic=protocol.MQTT_DEFAULT_TOPIC_PREFIX,
        allowed_commands=(),
        file_system_root="/tmp",
        process_timeout=DEFAULT_PROCESS_TIMEOUT,
        mqtt_queue_limit=10,
        reconnect_delay=DEFAULT_RECONNECT_DELAY,
        status_interval=DEFAULT_STATUS_INTERVAL,
        debug_logging=False,
        console_queue_limit_bytes=DEFAULT_CONSOLE_QUEUE_LIMIT_BYTES,
        mailbox_queue_limit=DEFAULT_MAILBOX_QUEUE_LIMIT,
        mailbox_queue_bytes_limit=DEFAULT_MAILBOX_QUEUE_BYTES_LIMIT,
    )

def test_configure_tls_disabled_returns_none(tmp_path: Path) -> None:
    config = _make_config(tls=False, cafile=str(tmp_path / "ca.pem"))
    assert mqtt_helper.configure_tls_context(config) is None


def test_configure_tls_missing_cafile_raises(tmp_path: Path) -> None:
    config = _make_config(tls=True, cafile=str(tmp_path / "missing.pem"))
    with pytest.raises(RuntimeError, match="MQTT TLS CA file missing"):
        mqtt_helper.configure_tls_context(config)


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
        check_hostname=True
    )

    def _fake_create_default_context(*_args, **_kwargs):
        return fake_context

    monkeypatch.setattr(ssl, "create_default_context", _fake_create_default_context)
    config = _make_config(tls=True, cafile=str(cafile))
    config.mqtt_certfile = str(tmp_path / "client.crt")
    config.mqtt_keyfile = str(tmp_path / "client.key")
    ctx = mqtt_helper.configure_tls_context(config)
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

    monkeypatch.setattr(ssl, "create_default_context", _boom)
    config = _make_config(tls=True, cafile=str(cafile))
    with pytest.raises(RuntimeError, match=r"TLS setup failed"):
        mqtt_helper.configure_tls_context(config)


@pytest.mark.asyncio
async def test_mqtt_task_requeues_on_publish_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config = _make_config(tls=False, cafile=None)
    state = create_runtime_state(config)
    service = BridgeService(config, state)
    await state.mqtt_publish_queue.put(
        QueuedPublish(
            topic_name=f"{protocol.MQTT_DEFAULT_TOPIC_PREFIX}/test/topic",
            payload=b"hello",
        )
    )

    class FakeClient:
        def __init__(self, **kwargs: Any) -> None: pass
        async def __aenter__(self) -> FakeClient: return self
        async def __aexit__(self, *args: Any) -> None: pass
        async def subscribe(self, *args: Any, **kwargs: Any) -> None: pass
        async def publish(self, *args: Any, **kwargs: Any) -> None:
            raise aiomqtt.MqttError("failed")

        @property
        def messages(self) -> Any:
            async def _iter():
                if False:
                    yield None
            return _iter()

    monkeypatch.setattr(aiomqtt, "Client", FakeClient)
    async def _cancel_sleep(*args, **kwargs):
        raise asyncio.CancelledError
    monkeypatch.setattr(asyncio, "sleep", _cancel_sleep)
    with pytest.raises(asyncio.CancelledError):
        await mqtt.mqtt_task(config, state, service)
    assert state.mqtt_publish_queue.qsize() == 1


@pytest.mark.asyncio
async def test_mqtt_publisher_loop_queue_full_on_cancel() -> None:
    config = _make_config(tls=False, cafile=None)
    config.mqtt_queue_limit = 1
    state = create_runtime_state(config)
    await state.mqtt_publish_queue.put(
        QueuedPublish(
            topic_name=f"{protocol.MQTT_DEFAULT_TOPIC_PREFIX}/test/topic",
            payload=b"hello",
        )
    )
    client = MagicMock(spec=aiomqtt.Client)
    client.publish = AsyncMock(side_effect=asyncio.CancelledError)
    task = asyncio.ensure_future(mqtt._mqtt_publisher_loop(state, client))
    await asyncio.sleep(0.01)
    task.cancel()
    try:
        await task
    except asyncio.CancelledError:
        pass
    assert state.mqtt_publish_queue.qsize() == 1


@pytest.mark.asyncio
async def test_mqtt_subscriber_loop_handles_mqtt_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config = _make_config(tls=False, cafile=None)
    state = create_runtime_state(config)
    service = BridgeService(config, state)

    class FakeClient:
        def __init__(self, **kwargs: Any) -> None: pass
        async def __aenter__(self) -> FakeClient: return self
        async def __aexit__(self, *args: Any) -> None: pass
        async def subscribe(self, *args: Any, **kwargs: Any) -> None: pass

        @property
        def messages(self) -> Any:
            async def _iter():
                yield MagicMock(topic="t", payload=b"p")
                raise aiomqtt.MqttError("boom")
            return _iter()

    monkeypatch.setattr(aiomqtt, "Client", FakeClient)
    async def _cancel_sleep(*args, **kwargs):
        raise asyncio.CancelledError
    monkeypatch.setattr(asyncio, "sleep", _cancel_sleep)
    with pytest.raises(asyncio.CancelledError):
        await mqtt.mqtt_task(config, state, service)


@pytest.mark.asyncio
async def test_mqtt_publisher_debug_logging() -> None:
    config = _make_config(tls=False, cafile=None)
    state = create_runtime_state(config)
    published: list[tuple[str, bytes]] = []

    class FakeClient:
        async def publish(self, topic, payload, **kwargs):
            published.append((str(topic), payload))

    client = FakeClient()
    await state.mqtt_publish_queue.put(
        QueuedPublish(
            topic_name=f"{protocol.MQTT_DEFAULT_TOPIC_PREFIX}/test/debug",
            payload=b"debug-test",
        )
    )
    with patch("mcubridge.transport.mqtt.logger") as mock_logger:
        mock_logger.isEnabledFor.return_value = True
        stop_task = asyncio.create_task(asyncio.sleep(0.1))

        async def run_loop():
            try:
                await mqtt._mqtt_publisher_loop(state, client)
            except asyncio.CancelledError:
                pass

        loop_task = asyncio.create_task(run_loop())
        await stop_task
        loop_task.cancel()
    assert len(published) == 1


@pytest.mark.asyncio
async def test_mqtt_subscriber_processes_message() -> None:
    config = _make_config(tls=False, cafile=None)
    state = create_runtime_state(config)
    service = BridgeService(config, state)
    msg_count = 0

    async def _mock_handle(msg):
        nonlocal msg_count
        msg_count += 1

    service.handle_mqtt_message = _mock_handle

    class FakeMsg:
        def __init__(self):
            self.topic = f"{protocol.MQTT_DEFAULT_TOPIC_PREFIX}/console/in"
            self.payload = b"test"
            self.qos = 0

    class FakeClient:
        def __init__(self):
            self.messages = self
        async def __aiter__(self):
            yield FakeMsg()

    client = FakeClient()
    task = asyncio.create_task(mqtt._mqtt_subscriber_loop(service, client))
    await asyncio.sleep(0.05)
    task.cancel()
    assert msg_count == 1


@pytest.mark.asyncio
async def test_mqtt_subscriber_empty_topic_skipped() -> None:
    config = _make_config(tls=False, cafile=None)
    state = create_runtime_state(config)
    service = BridgeService(config, state)
    msg_count = 0

    async def _mock_handle(msg):
        nonlocal msg_count
        msg_count += 1

    service.handle_mqtt_message = _mock_handle

    class FakeMsg:
        def __init__(self):
            self.topic = ""
            self.payload = b"p"

    class FakeClient:
        def __init__(self):
            self.messages = self
        async def __aiter__(self):
            yield FakeMsg()

    client = FakeClient()
    task = asyncio.create_task(mqtt._mqtt_subscriber_loop(service, client))
    await asyncio.sleep(0.05)
    task.cancel()
    assert msg_count == 0
