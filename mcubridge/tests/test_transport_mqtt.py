"""Unit tests for mcubridge.transport.mqtt."""

from __future__ import annotations
from mcubridge.transport.mqtt import MqttTransport

import os
import asyncio
import ssl
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import aiomqtt
import pytest
from mcubridge.config.settings import RuntimeConfig
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
    pass


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
