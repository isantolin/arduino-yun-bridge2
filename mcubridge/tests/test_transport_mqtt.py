"""Unit tests for mcubridge.transport.mqtt."""

from __future__ import annotations
from mcubridge.transport.mqtt import MqttTransport

import os
import asyncio
import contextlib
import ssl
from pathlib import Path
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


def _make_config(
    *,
    tls: bool,
    cafile: str | None,
    spool_dir: str = os.path.abspath(".tmp_tests/mcubridge-test-transport-spool"),
) -> RuntimeConfig:
    return RuntimeConfig(
        mqtt_user="user",
        mqtt_pass="pass",
        mqtt_tls=tls,
        mqtt_cafile=cafile,
        allowed_commands=(),
        mqtt_queue_limit=10,
        mqtt_spool_dir=spool_dir,
        serial_port="/dev/null",
        serial_shared_secret=b"s_e_c_r_e_t_mock",
        file_system_root=os.path.abspath(".tmp_tests/mcubridge-test-transport-fs"),
        allow_non_tmp_paths=True,
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

    fake_context = MagicMock(spec=ssl.SSLContext)
    fake_context.minimum_version = None
    fake_context.check_hostname = True
    fake_context.load_cert_chain = MagicMock()

    monkeypatch.setattr(
        ssl,
        "create_default_context",
        MagicMock(return_value=fake_context),
    )
    config = _make_config(tls=True, cafile=str(cafile))
    config.mqtt_certfile = str(tmp_path / "client.crt")
    config.mqtt_keyfile = str(tmp_path / "client.key")
    ctx = config.get_ssl_context()
    assert ctx is fake_context
    fake_context.load_cert_chain.assert_called_once_with(
        config.mqtt_certfile, config.mqtt_keyfile
    )


def test_configure_tls_wraps_ssl_errors(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    cafile = tmp_path / "ca.pem"
    cafile.write_text("not-a-real-ca")

    # [SIL-2] Use MagicMock for synchronous SSL context creation
    monkeypatch.setattr(
        ssl, "create_default_context", MagicMock(side_effect=ValueError("bad"))
    )
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

        transport = mqtt.MqttTransport(config, state)
        transport.stash_mqtt_message = AsyncMock(return_value=True)
        monkeypatch.setattr(
            mqtt.MqttTransport, "flush_mqtt_spool", AsyncMock(return_value=None)
        )

        # [SIL-2] Suppress warnings about unawaited coroutines during teardown
        with warnings.catch_warnings():
            warnings.filterwarnings(
                "ignore",
                category=RuntimeWarning,
                message="coroutine '.*' was never awaited",
            )
            task = asyncio.create_task(transport._publisher_loop(mock_client))  # type: ignore[reportPrivateUsage]

            # Wait for the mock to be called instead of using a list and event
            for _ in range(100):
                if transport.stash_mqtt_message.called:
                    break
                await asyncio.sleep(0.01)

            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task

        transport.stash_mqtt_message.assert_called_once()
        msg = transport.stash_mqtt_message.call_args[0][0]
        assert msg.topic_name == f"{protocol.MQTT_DEFAULT_TOPIC_PREFIX}/test/topic"
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
        client = AsyncMock(spec=aiomqtt.Client)
        client.publish = AsyncMock()

        await state.mqtt_publish_queue.put(
            QueuedPublish(
                topic_name=f"{protocol.MQTT_DEFAULT_TOPIC_PREFIX}/test/debug",
                payload=b"debug-test",
            )
        )
        with patch("mcubridge.transport.mqtt.logger") as mock_logger:
            mock_logger.is_enabled_for.return_value = True

            async def run_loop():
                try:
                    await transport._publisher_loop(client)  # type: ignore[reportPrivateUsage]
                except asyncio.CancelledError:
                    pass

            loop_task = asyncio.create_task(run_loop())

            # Wait for publish to be called
            for _ in range(50):
                if client.publish.called:
                    break
                await asyncio.sleep(0.1)

            loop_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await loop_task

        client.publish.assert_called_once()
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

        service.handle_mqtt_message = AsyncMock()

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
        service.handle_mqtt_message.assert_called_once_with(msg)
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

        service.handle_mqtt_message = AsyncMock()

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
        service.handle_mqtt_message.assert_not_called()
    finally:
        state.cleanup()
