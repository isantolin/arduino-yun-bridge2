"""Unit tests for mcubridge.transport.mqtt."""

from __future__ import annotations

import asyncio
import contextlib
import os
import ssl
from pathlib import Path
from types import SimpleNamespace
from typing import Any
import msgspec
from unittest.mock import AsyncMock, MagicMock

import aiomqtt
import pytest


from tests._helpers import make_test_config


def _make_config(
    *,
    tls: bool,
    cafile: str | None,
    spool_dir: str = os.path.abspath(".tmp_tests/mcubridge-test-transport-spool"),
) -> Any:
    return make_test_config(
        mqtt_user="user",
        mqtt_pass="pass",
        mqtt_tls=tls,
        mqtt_cafile=cafile,
        allowed_commands=(),
        mqtt_queue_limit=10,
        mqtt_spool_dir=spool_dir,
    )


@pytest.mark.asyncio
async def test_configure_tls_disabled_returns_none() -> None:
    config = _make_config(tls=False, cafile=None)
    assert config.get_ssl_context() is None


@pytest.mark.asyncio
async def test_configure_tls_loads_cert_chain_when_provided(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    cafile = Path(".tmp_tests/test-ca.pem")
    cafile.parent.mkdir(parents=True, exist_ok=True)
    cafile.write_text("not-a-real-ca")

    mock_context = MagicMock(spec=ssl.SSLContext)
    mock_create = MagicMock(return_value=mock_context)
    monkeypatch.setattr(ssl, "create_default_context", mock_create)

    config = _make_config(tls=True, cafile=str(cafile))
    ctx = config.get_ssl_context()

    assert ctx == mock_context
    mock_create.assert_called_once()
    assert mock_create.call_args[1]["cafile"] == str(cafile)


@pytest.mark.asyncio
async def test_configure_tls_missing_cafile_raises() -> None:
    config = _make_config(tls=True, cafile="/nonexistent/ca.pem")
    with pytest.raises(RuntimeError, match=r"MQTT TLS CA file missing"):
        config.get_ssl_context()


@pytest.mark.asyncio
async def test_configure_tls_wraps_ssl_errors(monkeypatch: pytest.MonkeyPatch) -> None:
    cafile = Path(".tmp_tests/test-ca.pem")
    cafile.parent.mkdir(parents=True, exist_ok=True)
    cafile.write_text("not-a-real-ca")

    # [SIL-2] Use MagicMock for synchronous SSL context creation
    monkeypatch.setattr(
        ssl, "create_default_context", MagicMock(side_effect=ValueError("bad"))
    )
    config = _make_config(tls=True, cafile=str(cafile))
    with pytest.raises(RuntimeError, match=r"TLS setup failed"):
        config.get_ssl_context()


@pytest.mark.asyncio
async def test_mqtt_publisher_loop_queue_full_on_cancel(
    runtime_config: Any,
) -> None:
    from mcubridge.state.context import create_runtime_state
    from mcubridge.transport.mqtt import MqttTransport

    state = create_runtime_state(runtime_config)
    transport = MqttTransport(runtime_config, state)
    mock_client = AsyncMock(spec=aiomqtt.Client)

    # Cancel the loop immediately
    task = asyncio.create_task(transport._publisher_loop(mock_client))  # type: ignore[reportPrivateUsage]
    task.cancel()
    with contextlib.suppress(asyncio.CancelledError):
        await task

    state.cleanup()


@pytest.mark.asyncio
async def test_mqtt_publisher_debug_logging(
    runtime_config: Any,
    caplog: pytest.LogCaptureFixture,
) -> None:
    from mcubridge.state.context import create_runtime_state
    from mcubridge.transport.mqtt import MqttTransport

    runtime_config = msgspec.structs.replace(runtime_config, debug=True)
    state = create_runtime_state(runtime_config)
    transport = MqttTransport(runtime_config, state)
    mock_client = AsyncMock(spec=aiomqtt.Client)

    # Trigger a publish failure to exercise log path
    mock_client.publish.side_effect = aiomqtt.MqttError("fail")

    from mcubridge.protocol.structures import QueuedPublish

    msg = QueuedPublish(topic_name="test", payload=b"data")
    await state.mqtt_publish_queue.put(msg)

    task = asyncio.create_task(transport._publisher_loop(mock_client))  # type: ignore[reportPrivateUsage]
    # Give it time to hit the exception
    await asyncio.sleep(0.1)
    task.cancel()
    with contextlib.suppress(asyncio.CancelledError):
        await task
        await task

    state.cleanup()


@pytest.mark.asyncio
async def test_mqtt_subscriber_loop_handles_mqtt_error(
    runtime_config: Any,
) -> None:
    from mcubridge.state.context import create_runtime_state
    from mcubridge.transport.mqtt import MqttTransport

    state = create_runtime_state(runtime_config)
    transport = MqttTransport(runtime_config, state)

    mock_client = MagicMock(spec=aiomqtt.Client)
    # [SIL-2] Use AsyncMock for async iterator
    mock_messages = AsyncMock()
    mock_messages.__aiter__.side_effect = aiomqtt.MqttError("stream fail")
    mock_client.messages = mock_messages

    with pytest.raises(aiomqtt.MqttError):
        await transport._subscriber_loop(mock_client)  # type: ignore[reportPrivateUsage]

    state.cleanup()


@pytest.mark.asyncio
async def test_mqtt_subscriber_processes_message(
    runtime_config: Any,
) -> None:
    from mcubridge.state.context import create_runtime_state
    from mcubridge.transport.mqtt import MqttTransport

    state = create_runtime_state(runtime_config)
    transport = MqttTransport(runtime_config, state)
    mock_service = AsyncMock()
    transport.set_service(mock_service)

    mock_client = MagicMock(spec=aiomqtt.Client)

    # [SIL-2] Simulate message stream
    fake_msg = MagicMock()
    fake_msg.topic = SimpleNamespace(value="test/topic")
    fake_msg.payload = b"payload"

    async def _msg_gen():
        yield fake_msg
        # Then raise to break loop
        raise asyncio.CancelledError()

    mock_client.messages = _msg_gen()

    with pytest.raises(asyncio.CancelledError):
        await transport._subscriber_loop(mock_client)  # type: ignore[reportPrivateUsage]

    mock_service.handle_mqtt_message.assert_called_once()
    state.cleanup()


@pytest.mark.asyncio
async def test_mqtt_subscriber_empty_topic_skipped(
    runtime_config: Any,
) -> None:
    from mcubridge.state.context import create_runtime_state
    from mcubridge.transport.mqtt import MqttTransport

    state = create_runtime_state(runtime_config)
    transport = MqttTransport(runtime_config, state)
    mock_service = AsyncMock()
    transport.set_service(mock_service)

    mock_client = MagicMock(spec=aiomqtt.Client)

    fake_msg = MagicMock()
    fake_msg.topic = ""  # Empty topic

    async def _msg_gen():
        yield fake_msg
        raise asyncio.CancelledError()

    mock_client.messages = _msg_gen()

    with pytest.raises(asyncio.CancelledError):
        await transport._subscriber_loop(mock_client)  # type: ignore[reportPrivateUsage]

    mock_service.handle_mqtt_message.assert_not_called()
    state.cleanup()
