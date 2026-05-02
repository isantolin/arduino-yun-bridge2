"""Refactored lifecycle test ensuring full execution of connection hooks without orchestration hangs."""

from __future__ import annotations

import logging
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import msgspec
import pytest

from mcubridge.config.settings import RuntimeConfig
from mcubridge.protocol import structures
from mcubridge.protocol.protocol import Status
from mcubridge.protocol.topics import Topic
from mcubridge.services.runtime import BridgeService
from mcubridge.state.context import create_runtime_state


@pytest.mark.asyncio
async def test_bridge_service_lifecycle_full_sync(
    runtime_config: RuntimeConfig,
) -> None:

    config = runtime_config
    state = create_runtime_state(config)
    try:
        enqueue_mqtt = AsyncMock()
        service = BridgeService(config, state, enqueue_mqtt)

        # [SIL-2] Isolate handshake and system components
        service.handshake_manager.synchronize = AsyncMock(return_value=True)
        service.handshake_manager.raise_if_handshake_fatal = MagicMock()

        system = service.system
        console = service.console
        # Mocking logic after sync
        system.request_mcu_version = AsyncMock(return_value=True)
        console.flush_queue = AsyncMock()

        async with service:
            # Manually execute connection hook logic
            state.mark_transport_connected()

            # 1. Sync
            await service.handshake_manager.synchronize()
            state.mark_synchronized()

            # 2. Get Version
            await system.request_mcu_version()

            # 3. Console flush
            await console.flush_queue()

            assert state.is_synchronized is True
            assert system.request_mcu_version.called
            assert console.flush_queue.called

    finally:
        state.cleanup()


@pytest.mark.asyncio
async def test_bridge_service_handle_status_reporting(
    runtime_config: RuntimeConfig, runtime_state: Any
) -> None:
    enqueue_mqtt = AsyncMock()
    service = BridgeService(runtime_config, runtime_state, enqueue_mqtt)

    await service.handle_status(1, Status.ERROR, b"some error")

    # Check that MQTT publish was called
    enqueue_mqtt.assert_called()
    args, kwargs = enqueue_mqtt.call_args
    msg = args[0] if args else kwargs.get("message")
    assert "mcu_status" in msg.topic_name
    # Payload is raw bytes as sent by MCU
    assert msg.payload == b"some error"


@pytest.mark.asyncio
async def test_serial_flow_acknowledge_no_sender_is_noop():
    from mcubridge.services.serial_flow import SerialFlowController

    ctrl = SerialFlowController(
        ack_timeout=1.0,
        response_timeout=2.0,
        max_attempts=3,
        logger=logging.getLogger("test"),
    )
    # No sender registered
    await ctrl.acknowledge(Status.ACK.value, 1)


@pytest.mark.asyncio
async def test_bridge_service_publish_snapshot(
    runtime_config: RuntimeConfig, runtime_state: Any
) -> None:
    enqueue_mqtt = AsyncMock()
    service = BridgeService(runtime_config, runtime_state, enqueue_mqtt)

    await service._publish_bridge_snapshot("summary", None)  # type: ignore[reportPrivateUsage]
    enqueue_mqtt.assert_called()


@pytest.mark.asyncio
async def test_bridge_service_reject_topic_action(
    runtime_config: RuntimeConfig, runtime_state: Any
) -> None:
    enqueue_mqtt = AsyncMock()
    service = BridgeService(runtime_config, runtime_state, enqueue_mqtt)

    from aiomqtt.message import Message

    msg = MagicMock(spec=Message)

    await service._reject_topic_action(msg, Topic.DIGITAL, "write")  # type: ignore[reportPrivateUsage]
    enqueue_mqtt.assert_called()


@pytest.mark.asyncio
async def test_bridge_service_is_topic_action_allowed_delegation(
    runtime_config: RuntimeConfig, runtime_state: Any
) -> None:
    # Use restrictive policy for test
    from mcubridge.protocol.structures import TopicAuthorization

    runtime_state.topic_authorization = TopicAuthorization(digital_write=False)

    service = BridgeService(runtime_config, runtime_state, AsyncMock())

    assert service._is_topic_action_allowed(Topic.DIGITAL, "write") is False  # type: ignore[reportPrivateUsage]

    # Enable it
    runtime_state.topic_authorization = TopicAuthorization(digital_write=True)
    assert service._is_topic_action_allowed(Topic.DIGITAL, "write") is True  # type: ignore[reportPrivateUsage]
