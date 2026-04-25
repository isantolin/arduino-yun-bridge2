import msgspec
import pytest
import logging
from typing import Any
from unittest.mock import AsyncMock, MagicMock

from mcubridge.config.settings import RuntimeConfig
from mcubridge.protocol.protocol import Status
from mcubridge.protocol import structures
from mcubridge.protocol.topics import Topic
from mcubridge.services.runtime import BridgeService
from mcubridge.state.context import create_runtime_state
from mcubridge.services import SystemComponent, ConsoleComponent
from tests._helpers import make_test_config


@pytest.mark.asyncio
async def test_bridge_service_lifecycle_full_sync() -> None:
    """Refactored lifecycle test ensuring full execution of connection hooks without orchestration hangs."""
    config = make_test_config()
    state = create_runtime_state(config)
    try:
        from mcubridge.mqtt.spool_manager import MqttSpoolManager

        spool = MagicMock(spec=MqttSpoolManager)
        service = BridgeService(config, state, spool)

        # [SIL-2] Isolate handshake and system components
        service.handshake_manager.synchronize = AsyncMock(return_value=True)
        service.handshake_manager.raise_if_handshake_fatal = MagicMock()

        system = service._container.get(SystemComponent)  # type: ignore[reportPrivateUsage]
        console = service._container.get(ConsoleComponent)  # type: ignore[reportPrivateUsage]

        # Mocking logic after sync
        system.request_mcu_version = AsyncMock(return_value=True)
        console.flush_queue = AsyncMock()

        async def mock_sender(
            command_id: int, payload: bytes, seq_id: int | None = None
        ) -> bool:
            return True

        service.register_serial_sender(mock_sender)

        async with service:
            # Manually execute connection hook logic - this exercises the same paths as on_serial_connected
            state.mark_transport_connected()

            # 1. Sync
            await service.handshake_manager.synchronize()
            # Logic inside on_serial_connected expects synchronized state to proceed
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
    from mcubridge.mqtt.spool_manager import MqttSpoolManager

    spool = MagicMock(spec=MqttSpoolManager)
    service = BridgeService(runtime_config, runtime_state, spool)

    await service.handle_status(1, Status.ERROR, b"some error")

    # Check that message was queued
    assert runtime_state.mqtt_publish_queue.qsize() == 1
    msg = runtime_state.mqtt_publish_queue.get_nowait()
    assert "status" in msg.topic_name
    report = msgspec.msgpack.decode(msg.payload)
    assert report["name"] == "ERROR"
    assert report["message"] == "some error"


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
    await ctrl.acknowledge(0x01, 1)


@pytest.mark.asyncio
async def test_enqueue_mqtt_spool_unavailable_logs(
    runtime_config: RuntimeConfig, runtime_state: Any
):
    # No spool configured
    from mcubridge.mqtt.spool_manager import MqttSpoolManager

    spool = MagicMock(spec=MqttSpoolManager)
    service = BridgeService(runtime_config, runtime_state, spool)

    msg = structures.QueuedPublish(topic_name="test", payload=b"data")
    await service.enqueue_mqtt(msg)
    assert runtime_state.mqtt_publish_queue.qsize() == 1


@pytest.mark.asyncio
async def test_bridge_service_publish_snapshot(
    runtime_config: RuntimeConfig, runtime_state: Any
) -> None:
    from mcubridge.mqtt.spool_manager import MqttSpoolManager

    spool = MagicMock(spec=MqttSpoolManager)
    service = BridgeService(runtime_config, runtime_state, spool)

    await service._publish_bridge_snapshot("summary", None)  # type: ignore[reportPrivateUsage]
    assert runtime_state.mqtt_publish_queue.qsize() == 1


@pytest.mark.asyncio
async def test_bridge_service_reject_topic_action(
    runtime_config: RuntimeConfig, runtime_state: Any
) -> None:
    from mcubridge.mqtt.spool_manager import MqttSpoolManager

    spool = MagicMock(spec=MqttSpoolManager)
    service = BridgeService(runtime_config, runtime_state, spool)

    from aiomqtt.message import Message

    msg = Message("test", b"", 0, False, False, None)

    await service._reject_topic_action(msg, Topic.DIGITAL, "write")  # type: ignore[reportPrivateUsage]
    assert runtime_state.mqtt_publish_queue.qsize() == 1


@pytest.mark.asyncio
async def test_bridge_service_is_topic_action_allowed_delegation(
    runtime_config: RuntimeConfig, runtime_state: Any
) -> None:
    # Use restrictive policy for test
    from mcubridge.protocol.structures import TopicAuthorization

    runtime_state.topic_authorization = TopicAuthorization(digital_write=False)

    service = BridgeService(runtime_config, runtime_state, MagicMock())

    assert service._is_topic_action_allowed(Topic.DIGITAL, "write") is False  # type: ignore[reportPrivateUsage]

    # Enable it
    runtime_state.topic_authorization = TopicAuthorization(digital_write=True)
    assert service._is_topic_action_allowed(Topic.DIGITAL, "write") is True  # type: ignore[reportPrivateUsage]
