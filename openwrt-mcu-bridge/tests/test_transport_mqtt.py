"""Unit tests for mcubridge.transport.mqtt."""

from __future__ import annotations

import asyncio
from pathlib import Path
from types import SimpleNamespace
import pytest

import aiomqtt

from mcubridge.config.settings import RuntimeConfig
from mcubridge.config.const import (
    DEFAULT_CONSOLE_QUEUE_LIMIT_BYTES,
    DEFAULT_MAILBOX_QUEUE_BYTES_LIMIT,
    DEFAULT_MAILBOX_QUEUE_LIMIT,
    DEFAULT_MQTT_PORT,
    DEFAULT_PROCESS_TIMEOUT,
    DEFAULT_RECONNECT_DELAY,
    DEFAULT_STATUS_INTERVAL,
)
from mcubridge.mqtt.messages import QueuedPublish
from mcubridge.protocol import protocol
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
                # Long sleep to prevent yielding None before cancel
                while True:
                    await asyncio.sleep(10)
                    yield None  # Never reached

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


@pytest.mark.asyncio
async def test_mqtt_publisher_loop_queue_full_on_cancel(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """Test publisher handles queue full during cancellation by logging warning."""
    config = _make_config(tls=False, cafile=None)
    # Create a tiny queue that will overflow
    config.mqtt_queue_limit = 1
    state = create_runtime_state(config)
    service = BridgeService(config, state)

    # Seed one message to be published when cancelled
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
            raise asyncio.CancelledError()  # Simulate cancellation during publish

        @property
        def messages(self):
            async def _iter():
                # Sleep briefly then check for cancellation
                try:
                    while True:
                        await asyncio.sleep(0.01)
                except asyncio.CancelledError:
                    return  # Allow clean exit when TaskGroup cancels
                yield None  # pragma: no cover

            return _iter()

    monkeypatch.setattr(mqtt.aiomqtt, "Client", FakeClient)

    with pytest.raises(asyncio.CancelledError):
        await mqtt.mqtt_task(config, state, service)


@pytest.mark.asyncio
async def test_mqtt_subscriber_loop_handles_mqtt_error(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """Test subscriber loop handles aiomqtt.MqttError gracefully."""
    config = _make_config(tls=False, cafile=None)
    state = create_runtime_state(config)
    service = BridgeService(config, state)

    class FakeClient:
        def __init__(self, **_kwargs):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, exc_type, exc, _tb):
            return False

        async def subscribe(self, topic: str, qos: int = 0):
            pass

        async def publish(self, *_args, **_kwargs):
            pass

        @property
        def messages(self):
            async def _iter():
                # Raise MQTT error after first iteration
                raise aiomqtt.MqttError("connection lost")
                yield  # pragma: no cover

            return _iter()

    monkeypatch.setattr(mqtt.aiomqtt, "Client", FakeClient)

    # Stop after the first reconnect attempt.
    async def _cancel_sleep(_seconds: float):
        raise asyncio.CancelledError

    monkeypatch.setattr(mqtt.asyncio, "sleep", _cancel_sleep)

    with pytest.raises(asyncio.CancelledError):
        await mqtt.mqtt_task(config, state, service)


@pytest.mark.asyncio
async def test_mqtt_publisher_debug_logging(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
    tmp_path: Path,
) -> None:
    """Test publisher loop debug logging: directly test _mqtt_publisher_loop."""
    import mcubridge.transport.mqtt as mqtt_module

    config = _make_config(tls=False, cafile=None)
    state = create_runtime_state(config)

    published: list[tuple[str, bytes]] = []

    class FakeOutboundClient:
        async def publish(self, topic: str, payload: bytes, **_kwargs) -> None:
            published.append((topic, payload))
            raise asyncio.CancelledError()

    # Put a message in the queue
    await state.mqtt_publish_queue.put(
        QueuedPublish(
            topic_name=f"{protocol.MQTT_DEFAULT_TOPIC_PREFIX}/test/debug",
            payload=b"debug-test",
        )
    )

    caplog.set_level("DEBUG")
    fake_client = FakeOutboundClient()

    with pytest.raises(asyncio.CancelledError):
        await mqtt_module._mqtt_publisher_loop(state, fake_client)  # type: ignore[arg-type]

    assert len(published) == 1
    assert published[0][1] == b"debug-test"


@pytest.mark.asyncio
async def test_mqtt_subscriber_processes_message(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """Test subscriber loop processes incoming messages."""
    config = _make_config(tls=False, cafile=None)
    state = create_runtime_state(config)
    service = BridgeService(config, state)

    msg_count = 0

    class FakeMsg:
        def __init__(self):
            self.topic = f"{protocol.MQTT_DEFAULT_TOPIC_PREFIX}/console/in"
            self.payload = b"test"
            self.qos = 0
            self.retain = False
            self.properties = None

    class FakeClient:
        def __init__(self, **_kwargs):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, exc_type, exc, _tb):
            return False

        async def subscribe(self, topic: str, qos: int = 0):
            pass

        async def publish(self, *_args, **_kwargs):
            pass

        @property
        def messages(self):
            async def _iter():
                nonlocal msg_count
                while msg_count < 1:
                    msg_count += 1
                    yield FakeMsg()
                raise asyncio.CancelledError()

            return _iter()

    monkeypatch.setattr(mqtt.aiomqtt, "Client", FakeClient)

    with pytest.raises(asyncio.CancelledError):
        await mqtt.mqtt_task(config, state, service)

    assert msg_count == 1


@pytest.mark.asyncio
async def test_mqtt_subscriber_empty_topic_skipped(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """Test subscriber loop skips messages with empty topics."""
    config = _make_config(tls=False, cafile=None)
    state = create_runtime_state(config)
    service = BridgeService(config, state)

    msg_count = 0

    class FakeMsgEmpty:
        def __init__(self):
            self.topic = ""  # Empty topic
            self.payload = b"ignored"
            self.qos = 0
            self.retain = False
            self.properties = None

    class FakeClient:
        def __init__(self, **_kwargs):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, exc_type, exc, _tb):
            return False

        async def subscribe(self, topic: str, qos: int = 0):
            pass

        async def publish(self, *_args, **_kwargs):
            pass

        @property
        def messages(self):
            async def _iter():
                nonlocal msg_count
                while msg_count < 1:
                    msg_count += 1
                    yield FakeMsgEmpty()
                raise asyncio.CancelledError()

            return _iter()

    monkeypatch.setattr(mqtt.aiomqtt, "Client", FakeClient)

    with pytest.raises(asyncio.CancelledError):
        await mqtt.mqtt_task(config, state, service)
