"""Unit tests for mcubridge MQTT logic and spool management."""

from __future__ import annotations

import asyncio
import contextlib
from unittest.mock import AsyncMock, MagicMock, patch

import aiomqtt
import pytest
from mcubridge.config.settings import RuntimeConfig
from mcubridge.protocol.structures import QueuedPublish
from mcubridge.services.runtime import BridgeService
from mcubridge.state.context import create_runtime_state
from mcubridge.mqtt.logic import mqtt_publisher_loop, mqtt_subscriber_loop
from mcubridge.mqtt.spool_manager import MqttSpoolManager

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


@pytest.mark.asyncio
async def test_mqtt_publisher_loop_publishes_from_queue() -> None:
    config = _make_config(tls=False, cafile=None)
    state = create_runtime_state(config)
    try:
        msg = QueuedPublish(topic_name="test/topic", payload=b"hello")
        await state.mqtt_publish_queue.put(msg)

        mock_client = AsyncMock(spec=aiomqtt.Client)

        # Run loop for a short period
        task = asyncio.create_task(mqtt_publisher_loop(mock_client, state))
        await asyncio.sleep(0.05)
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await task

        mock_client.publish.assert_called_once()
        args, kwargs = mock_client.publish.call_args
        assert args[0] == "test/topic"
        assert args[1] == b"hello"
    finally:
        state.cleanup()


@pytest.mark.asyncio
async def test_mqtt_subscriber_loop_dispatches_to_service() -> None:
    config = _make_config(tls=False, cafile=None)
    state = create_runtime_state(config)

    # Mock spool manager to avoid disk access in this test
    mock_spool = MagicMock(spec=MqttSpoolManager)
    service = BridgeService(config, state, mock_spool)

    mock_client = AsyncMock(spec=aiomqtt.Client)

    msg = MagicMock(spec=aiomqtt.Message)
    msg.topic = "test/topic"
    msg.payload = b"test-payload"

    async def _iter_messages():
        yield msg
        # Stay suspended until cancelled
        while True:
            await asyncio.sleep(1)

    mock_client.messages = _iter_messages()

    with patch.object(
        service, "handle_mqtt_message", new_callable=AsyncMock
    ) as mock_handle:
        task = asyncio.create_task(mqtt_subscriber_loop(mock_client, service))
        await asyncio.sleep(0.05)
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await task

        mock_handle.assert_called_once_with(msg)


@pytest.mark.asyncio
async def test_spool_manager_stash_and_flush() -> None:
    config = _make_config(tls=False, cafile=None)
    config.mqtt_spool_limit = 100  # Ensure limit > 0
    state = create_runtime_state(config)
    try:
        manager = MqttSpoolManager(state)
        manager.initialize()

        # Ensure it's not degraded before starting
        state.mqtt_spool_degraded = False

        msg = QueuedPublish(topic_name="spool/test", payload=b"persistent")

        # Stash it
        ok = await manager.stash(msg)
        assert ok is True
        assert state.mqtt_spooled_messages == 1

        # Flush it into the queue
        await manager.flush()
        assert state.mqtt_publish_queue.qsize() == 1

        queued = await state.mqtt_publish_queue.get()
        assert queued.topic_name == "spool/test"
        assert queued.payload == b"persistent"
        # Check that bridge-spooled property was added
        assert any(k == "bridge-spooled" for k, v in queued.user_properties)
    finally:
        state.cleanup()


@pytest.mark.asyncio
async def test_service_enqueue_mqtt_with_overflow() -> None:
    config = _make_config(tls=False, cafile=None)
    config.mqtt_queue_limit = 1
    state = create_runtime_state(config)
    try:
        mock_spool = MagicMock(spec=MqttSpoolManager)
        mock_spool.stash = AsyncMock(return_value=True)
        service = BridgeService(config, state, mock_spool)

        msg1 = QueuedPublish(topic_name="t1", payload=b"p1")
        msg2 = QueuedPublish(topic_name="t2", payload=b"p2")

        await service.enqueue_mqtt(msg1)
        assert state.mqtt_publish_queue.qsize() == 1

        # This should trigger overflow spooling of msg1 and queue msg2
        await service.enqueue_mqtt(msg2)

        assert state.mqtt_publish_queue.qsize() == 1
        mock_spool.stash.assert_called_once_with(msg1)

        final_msg = await state.mqtt_publish_queue.get()
        assert final_msg.topic_name == "t2"
    finally:
        state.cleanup()
