"""Extra unit tests for BridgeService lifecycle and edge cases (SIL-2)."""

from __future__ import annotations

import asyncio
import os
import time
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from mcubridge.config.settings import RuntimeConfig
from mcubridge.protocol.protocol import Status
from mcubridge.protocol.topics import Topic
from mcubridge.services import (
    ConsoleComponent,
    SystemComponent,
)
from mcubridge.services.runtime import BridgeService
from mcubridge.state.context import create_runtime_state
from mcubridge.transport.mqtt import MqttTransport
from tests.mqtt_helpers import make_inbound_message


@pytest.mark.asyncio
async def test_runtime_on_serial_connected_errors() -> None:
    config = RuntimeConfig(
        serial_shared_secret=b"secret_1234",
        file_system_root=f"/tmp/mcubridge-test-{os.getpid()}-{time.time_ns()}",
    )
    state = create_runtime_state(config)
    try:
        mqtt_mock = MagicMock(spec=MqttTransport)
        mqtt_mock.enqueue_mqtt = AsyncMock()
        mqtt_mock.publish = AsyncMock()
        mqtt_mock.is_topic_action_allowed = MagicMock(return_value=True)
        mqtt_mock.reject_topic_action = AsyncMock()
        mqtt_mock.publish_bridge_snapshot = AsyncMock()

        service = BridgeService(config, state, mqtt_mock)

        # Side effect to update state when synchronize is called
        async def _mock_sync():
            state.mark_synchronized()
            return True
        service.handshake_manager.synchronize = AsyncMock(side_effect=_mock_sync)

        # Retrieve the real components from the container
        system = service._container.get(SystemComponent)  # type: ignore[reportPrivateUsage]
        console = service._container.get(ConsoleComponent)  # type: ignore[reportPrivateUsage]
        
        # Patch them
        with patch.object(system, "request_mcu_version", new_callable=AsyncMock) as mock_version, \
             patch.object(console, "flush_queue", new_callable=AsyncMock) as mock_flush:
            
            # 1. Error requesting version
            mock_version.side_effect = RuntimeError("fail")
            await service.on_serial_connected()
            assert mock_version.called

            # 2. Error flushing console
            mock_flush.side_effect = ValueError("boom")
            # reset mock_version
            mock_version.side_effect = None
            await service.on_serial_connected()
            assert mock_flush.called
    finally:
        state.cleanup()


@pytest.mark.asyncio
async def test_runtime_handle_mqtt_message_dispatch_error() -> None:
    config = RuntimeConfig(serial_shared_secret=b"secret_1234")
    state = create_runtime_state(config)
    try:
        mqtt_mock = MagicMock()
        mqtt_mock.enqueue_mqtt = AsyncMock()
        mqtt_mock.publish = AsyncMock()
        mqtt_mock.is_topic_action_allowed = MagicMock(return_value=True)
        mqtt_mock.reject_topic_action = AsyncMock()
        mqtt_mock.publish_bridge_snapshot = AsyncMock()

        service = BridgeService(config, state, mqtt_mock)
        service.dispatcher.dispatch_mqtt_message = AsyncMock(
            side_effect=RuntimeError("dispatch fail")
        )

        from tests.mqtt_helpers import make_inbound_message

        inbound = make_inbound_message("br/system/status/get", b"")
        # Should catch and log, not raise
        await service.handle_mqtt_message(inbound)
    finally:
        state.cleanup()


@pytest.mark.asyncio
async def test_runtime_reject_topic_action_properties() -> None:
    config = RuntimeConfig(serial_shared_secret=b"secret_1234")
    state = create_runtime_state(config)
    try:
        mqtt_mock = MagicMock(spec=MqttTransport)
        mqtt_mock.enqueue_mqtt = AsyncMock()
        mqtt_mock.publish = AsyncMock()
        mqtt_mock.reject_topic_action = AsyncMock()

        service = BridgeService(config, state, mqtt_mock)
        from mcubridge.protocol.topics import Topic
        from tests.mqtt_helpers import make_inbound_message

        inbound = make_inbound_message("br/system/cmd", b"")
        inbound.properties = MagicMock()
        inbound.properties.ResponseTopic = "resp"
        inbound.properties.CorrelationData = b"cid"

        await service.mqtt_flow.reject_topic_action(inbound, Topic.SYSTEM, "action")
        mqtt_mock.reject_topic_action.assert_called_with(inbound, Topic.SYSTEM, "action")
    finally:
        state.cleanup()


@pytest.mark.asyncio
async def test_runtime_publish_bridge_snapshot_handshake() -> None:
    config = RuntimeConfig(serial_shared_secret=b"secret_1234")
    state = create_runtime_state(config)
    try:
        mqtt_mock = MagicMock(spec=MqttTransport)
        mqtt_mock.enqueue_mqtt = AsyncMock()
        mqtt_mock.publish = AsyncMock()
        mqtt_mock.publish_bridge_snapshot = AsyncMock()

        service = BridgeService(config, state, mqtt_mock)

        from tests.mqtt_helpers import make_inbound_message

        inbound = make_inbound_message("br/s/b/h/get", b"")
        inbound.properties = MagicMock()
        inbound.properties.ResponseTopic = "reply"

        await service.mqtt_flow.publish_bridge_snapshot("handshake", inbound)
        mqtt_mock.publish_bridge_snapshot.assert_called_with("handshake", inbound)
    finally:
        state.cleanup()
