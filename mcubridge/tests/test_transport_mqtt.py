"""Tests for the MQTT core link and transport management."""

from __future__ import annotations

import asyncio
import contextlib
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
import aiomqtt
from mcubridge.config.settings import RuntimeConfig
from mcubridge.state.context import create_runtime_state
from mcubridge.transport.mqtt import MqttTransport
from mcubridge.protocol.structures import QueuedPublish


@pytest.fixture
def runtime_state(runtime_config: RuntimeConfig):
    return create_runtime_state(runtime_config)


@pytest.mark.asyncio
async def test_mqtt_transport_init(runtime_config, runtime_state):
    transport = MqttTransport(runtime_config, runtime_state)
    assert transport.state == runtime_state
    assert transport.config == runtime_config


@pytest.mark.asyncio
async def test_mqtt_publisher_loop_consumes_queue(runtime_config, runtime_state):
    transport = MqttTransport(runtime_config, runtime_state)
    client = AsyncMock(spec=aiomqtt.Client)

    msg = QueuedPublish(topic_name="test/topic", payload=b"hello")
    await runtime_state.mqtt_publish_queue.put(msg)

    # Mock spool_manager to avoid DB issues in tests
    with patch("mcubridge.mqtt.spool_manager.flush_spool", new_callable=AsyncMock):
        task = asyncio.create_task(transport._publisher_loop(client))
        await asyncio.sleep(0.1)
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await task

    assert client.publish.called
    args, kwargs = client.publish.call_args
    assert args[0] == "test/topic"
    assert args[1] == b"hello"


@pytest.mark.asyncio
async def test_mqtt_subscriber_loop_calls_service(runtime_config, runtime_state):
    transport = MqttTransport(runtime_config, runtime_state)
    service = MagicMock()
    service.handle_mqtt_message = AsyncMock()
    transport.set_service(service)

    client = AsyncMock(spec=aiomqtt.Client)
    mock_msg = MagicMock(spec=aiomqtt.Message)
    mock_msg.topic = "test/in"

    async def mock_messages():
        yield mock_msg

    client.messages = mock_messages()

    task = asyncio.create_task(transport._subscriber_loop(client))
    await asyncio.sleep(0.1)
    task.cancel()
    with contextlib.suppress(asyncio.CancelledError):
        await task

    assert service.handle_mqtt_message.called


@pytest.mark.asyncio
async def test_mqtt_publish_shim_delegates_to_atomic(runtime_config, runtime_state):
    transport = MqttTransport(runtime_config, runtime_state)
    with patch(
        "mcubridge.transport.mqtt.atomic_publish", new_callable=AsyncMock
    ) as mock_atomic:
        await transport.publish("t", b"p")
        assert mock_atomic.called
        args, kwargs = mock_atomic.call_args
        assert args[1] == "t"
        assert args[2] == b"p"
