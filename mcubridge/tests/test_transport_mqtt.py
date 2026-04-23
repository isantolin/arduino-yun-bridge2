"""Unit tests for mcubridge.transport.mqtt."""

from __future__ import annotations
from mcubridge.transport.mqtt import MqttTransport

import asyncio
import contextlib
import ssl
from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast
from unittest.mock import AsyncMock, MagicMock, patch

import aiomqtt
import pytest
import warnings
from mcubridge.config.settings import RuntimeConfig
from mcubridge.protocol.structures import QueuedPublish
from mcubridge.protocol import protocol
from mcubridge.services.runtime import BridgeService
from mcubridge.state.context import create_runtime_state
from mcubridge.transport import mqtt

from tests._helpers import make_test_config


def _make_config(
    *,
    tls: bool,
    cafile: str | None,
    spool_dir: str = ".tmp_tests/mcubridge-test-transport-spool",
) -> RuntimeConfig:
    return make_test_config(
        mqtt_user="user",
        mqtt_pass="pass",
        mqtt_tls=tls,
        mqtt_cafile=cafile,
        allowed_commands=(),
        mqtt_queue_limit=10,
        mqtt_spool_dir=spool_dir,
    )


def test_configure_tls_disabled_returns_none(tmp_path: Path) -> None:
    config = _make_config(tls=False, cafile=str(tmp_path / "ca.pem"))
    assert config.get_ssl_context() is None


def test_configure_tls_missing_cafile_raises(tmp_path: Path) -> None:
    config = _make_config(tls=True, cafile=str(tmp_path / "missing.pem"))
    with pytest.raises(RuntimeError, match="MQTT TLS CA file missing"):
        config.get_ssl_context()


def test_configure_tls_loads_cert_chain_when_provided(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    cafile = tmp_path / "ca.pem"
    cafile.write_text("not-a-real-ca")
    calls: list[tuple[str, str]] = []

    fake_context = SimpleNamespace(
        minimum_version=None,
        load_cert_chain=lambda certfile, keyfile: calls.append(  # type: ignore[reportUnknownLambdaType]
            (cast(str, certfile), cast(str, keyfile))
        ),  # type: ignore[reportUnknownArgumentType]
        check_hostname=True,
    )

    monkeypatch.setattr(
        ssl,
        "create_default_context",
        lambda *_args, **_kwargs: fake_context,  # type: ignore[reportUnknownLambdaType]
    )
    config = _make_config(tls=True, cafile=str(cafile))
    config.mqtt_certfile = str(tmp_path / "client.crt")
    config.mqtt_keyfile = str(tmp_path / "client.key")
    ctx = config.get_ssl_context()
    assert ctx is fake_context
    assert calls == [(config.mqtt_certfile, config.mqtt_keyfile)]


def test_configure_tls_wraps_ssl_errors(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    cafile = tmp_path / "ca.pem"
    cafile.write_text("not-a-real-ca")

    # [SIL-2] Use MagicMock for synchronous SSL context creation
    monkeypatch.setattr(ssl, "create_default_context", MagicMock(side_effect=ValueError("bad")))
    config = _make_config(tls=True, cafile=str(cafile))
    with pytest.raises(RuntimeError, match=r"TLS setup failed"):
        config.get_ssl_context()


@pytest.mark.asyncio
async def test_mqtt_task_requeues_on_publish_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config = _make_config(tls=False, cafile=None)
    state = create_runtime_state(config)
    try:
        await state.mqtt_publish_queue.put(
            QueuedPublish(
                topic_name=f"{protocol.MQTT_DEFAULT_TOPIC_PREFIX}/test/topic",
                payload=b"hello",
            )
        )

        mock_client = AsyncMock(spec=aiomqtt.Client)
        mock_client.publish = AsyncMock(side_effect=aiomqtt.MqttError("failed"))
        monkeypatch.setattr(aiomqtt, "Client", MagicMock(return_value=mock_client))
        monkeypatch.setattr(
            mqtt.tenacity,
            "retry",
            lambda *args, **kwargs: lambda fn: fn,  # type: ignore[reportUnknownLambdaType]
        )
        stash_calls: list[QueuedPublish] = []
        stashed = asyncio.Event()

        async def _stash(self: Any, message: QueuedPublish) -> bool:
            del self
            stash_calls.append(message)
            stashed.set()
            return True

        monkeypatch.setattr(mqtt.MqttTransport, "stash_mqtt_message", _stash)
        monkeypatch.setattr(mqtt.MqttTransport, "flush_mqtt_spool", AsyncMock(return_value=None))

        transport = mqtt.MqttTransport(config, state)
        # [SIL-2] Suppress warnings about unawaited coroutines during teardown
        with warnings.catch_warnings():
            warnings.filterwarnings("ignore", category=RuntimeWarning, message="coroutine '.*' was never awaited")
            task = asyncio.create_task(transport._publisher_loop(mock_client))  # type: ignore[reportPrivateUsage]

            await asyncio.wait_for(stashed.wait(), timeout=1.0)
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task

        assert len(stash_calls) == 1
        assert stash_calls[0].topic_name == f"{protocol.MQTT_DEFAULT_TOPIC_PREFIX}/test/topic"
        assert state.mqtt_publish_queue.qsize() == 0
    finally:
        state.cleanup()


@pytest.mark.asyncio
async def test_mqtt_publisher_loop_queue_full_on_cancel() -> None:
    config = _make_config(tls=False, cafile=None)
    config.mqtt_queue_limit = 1
    state = create_runtime_state(config)
    try:
        transport = mqtt.MqttTransport(config, state)

        await state.mqtt_publish_queue.put(
            QueuedPublish(
                topic_name=f"{protocol.MQTT_DEFAULT_TOPIC_PREFIX}/test/topic",
                payload=b"hello",
            )
        )
        client = AsyncMock(spec=aiomqtt.Client)
        client.publish = AsyncMock(side_effect=asyncio.CancelledError)

        task = asyncio.create_task(transport._publisher_loop(client))  # type: ignore[reportPrivateUsage]
        await asyncio.sleep(0.01)
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass
        assert state.mqtt_publish_queue.qsize() == 1
    finally:
        state.cleanup()


@pytest.mark.asyncio
async def test_mqtt_subscriber_loop_handles_mqtt_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config = _make_config(tls=False, cafile=None)
    state = create_runtime_state(config)
    try:
        BridgeService(config, state, MqttTransport(config, state))
        transport = mqtt.MqttTransport(config, state)

        client = AsyncMock(spec=aiomqtt.Client)

        async def _iter():
            # [SIL-2] Use spec=aiomqtt.Message for high fidelity
            msg = MagicMock(spec=aiomqtt.Message)
            msg.topic = "t"
            msg.payload = b"p"
            yield msg
            raise aiomqtt.MqttError("boom")

        client.messages = _iter()

        with pytest.raises(aiomqtt.MqttError, match="boom"):
            await transport._subscriber_loop(client)  # type: ignore[reportPrivateUsage]
    finally:
        state.cleanup()


@pytest.mark.timeout(5)
@pytest.mark.asyncio
async def test_mqtt_publisher_debug_logging() -> None:
    config = _make_config(tls=False, cafile=None)
    state = create_runtime_state(config)
    try:
        transport = mqtt.MqttTransport(config, state)
        published: list[tuple[str, bytes]] = []

        client = AsyncMock(spec=aiomqtt.Client)

        async def mock_publish(topic: Any, payload: Any, **kwargs: Any):
            published.append((str(topic), payload))

        client.publish = AsyncMock(side_effect=mock_publish)
        await state.mqtt_publish_queue.put(
            QueuedPublish(
                topic_name=f"{protocol.MQTT_DEFAULT_TOPIC_PREFIX}/test/debug",
                payload=b"debug-test",
            )
        )
        with patch("mcubridge.transport.mqtt.logger") as mock_logger:
            mock_logger.is_enabled_for.return_value = True

            async def wait_publish():
                for _ in range(20):
                    if published:
                        break
                    await asyncio.sleep(0.1)

            stop_task = asyncio.create_task(wait_publish())

            async def run_loop():
                try:
                    await transport._publisher_loop(client)  # type: ignore[reportPrivateUsage]
                except asyncio.CancelledError:
                    pass

            loop_task = asyncio.create_task(run_loop())
            await stop_task
            loop_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await loop_task
        assert len(published) == 1
    finally:
        state.cleanup()


@pytest.mark.asyncio
async def test_mqtt_subscriber_processes_message() -> None:
    config = _make_config(tls=False, cafile=None)
    state = create_runtime_state(config)
    try:
        service = BridgeService(config, state, MqttTransport(config, state))
        transport = mqtt.MqttTransport(config, state)
        transport.set_service(service)
        msg_count = 0

        async def _mock_handle(msg: Any):
            nonlocal msg_count
            msg_count += 1

        service.handle_mqtt_message = _mock_handle  # type: ignore[reportAttributeAccessIssue]

        client = AsyncMock(spec=aiomqtt.Client)
        msg = MagicMock(spec=aiomqtt.Message)
        msg.topic = f"{protocol.MQTT_DEFAULT_TOPIC_PREFIX}/console/in"
        msg.payload = b"test"
        msg.qos = 0

        async def _iter():
            yield msg

        client.messages = _iter()
        task = asyncio.create_task(transport._subscriber_loop(client))  # type: ignore[reportPrivateUsage]
        await asyncio.sleep(0.05)
        task.cancel()
        assert msg_count == 1
    finally:
        state.cleanup()


@pytest.mark.asyncio
async def test_mqtt_subscriber_empty_topic_skipped() -> None:
    config = _make_config(tls=False, cafile=None)
    state = create_runtime_state(config)
    try:
        service = BridgeService(config, state, MqttTransport(config, state))
        transport = mqtt.MqttTransport(config, state)
        transport.set_service(service)
        msg_count = 0

        async def _mock_handle(msg: Any):
            nonlocal msg_count
            msg_count += 1

        service.handle_mqtt_message = _mock_handle  # type: ignore[reportAttributeAccessIssue]

        client = AsyncMock(spec=aiomqtt.Client)
        msg = MagicMock(spec=aiomqtt.Message)
        msg.topic = ""
        msg.payload = b"p"

        async def _iter():
            yield msg

        client.messages = _iter()
        task = asyncio.create_task(transport._subscriber_loop(client))  # type: ignore[reportPrivateUsage]
        await asyncio.sleep(0.05)
        task.cancel()
        assert msg_count == 0
    finally:
        state.cleanup()
