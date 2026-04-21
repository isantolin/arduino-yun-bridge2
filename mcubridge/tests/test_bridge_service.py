import asyncio
import msgspec
import pytest
import logging
from typing import Any
from unittest.mock import AsyncMock, MagicMock

from mcubridge.config.settings import RuntimeConfig
from mcubridge.protocol.protocol import Command, Status
from mcubridge.protocol import structures
from mcubridge.protocol.topics import Topic
from mcubridge.services.runtime import BridgeService
from mcubridge.state.context import create_runtime_state
from mcubridge.transport.mqtt import MqttTransport
from tests._helpers import make_test_config


@pytest.mark.skip(reason="Crashes worker; requires deeper isolation refactor")
@pytest.mark.asyncio
async def test_bridge_service_lifecycle_full_sync() -> None:
    """End-to-end test of the BridgeService lifecycle."""
    config = make_test_config()
    state = create_runtime_state(config)
    try:
        mqtt = MqttTransport(config, state)
        service = BridgeService(config, state, mqtt)

        # [MIL-SPEC] Handshake parameters
        nonce = bytes(range(16))
        from mcubridge.services.handshake import SerialHandshakeManager

        our_tag = SerialHandshakeManager.calculate_handshake_tag(config.serial_shared_secret, nonce)
        sync_resp = msgspec.msgpack.encode(structures.LinkSyncPacket(nonce=nonce, tag=our_tag))

        async def mock_sender(command_id: int, payload: bytes, seq_id: int | None = None) -> bool:
            # Inject responses using service-managed background tasks for stability
            if command_id == Command.CMD_LINK_SYNC.value:
                # We need to wait for on_serial_connected to be in synchronize()
                # But since this is mock_sender called BY synchronize, we must use background
                asyncio.create_task(service.handle_mcu_frame(Command.CMD_LINK_SYNC_RESP.value, 0, sync_resp))
            elif command_id == Command.CMD_GET_VERSION.value:
                v_resp = msgspec.msgpack.encode(structures.VersionResponsePacket(major=1, minor=2, patch=3))
                asyncio.create_task(service.handle_mcu_frame(Command.CMD_GET_VERSION_RESP.value, 0, v_resp))
            return True

        service.register_serial_sender(mock_sender)

        async with service:
            # Wait for handshake completion
            await service.on_serial_connected()

            # Wait for sync with timeout
            for _ in range(50):
                if state.is_synchronized:
                    break
                await asyncio.sleep(0.05)

            assert state.is_synchronized is True
            # Version might take a bit more due to 2s delay in handshake.py
            for _ in range(50):
                if state.mcu_version == (1, 2, 3):
                    break
                await asyncio.sleep(0.1)

            assert state.mcu_version == (1, 2, 3)

    finally:
        # Avoid closing loop issues by ensuring all tasks are done
        await asyncio.sleep(0.1)
        state.cleanup()


@pytest.mark.asyncio
async def test_bridge_service_handle_status_reporting(runtime_config: RuntimeConfig, runtime_state: Any) -> None:
    mqtt = MagicMock()
    mqtt.publish = AsyncMock()
    service = BridgeService(runtime_config, runtime_state, mqtt)

    await service.handle_status(1, Status.ERROR, b"some error")

    # Check that MQTT publish was called
    mqtt.publish.assert_called()
    args, kwargs = mqtt.publish.call_args
    assert "status" in kwargs["topic"]
    # Payload is msgpacked
    report = msgspec.msgpack.decode(kwargs["payload"])
    assert report["name"] == "ERROR"
    assert report["message"] == "some error"


@pytest.mark.asyncio
async def test_serial_flow_acknowledge_no_sender_is_noop():
    from mcubridge.services.serial_flow import SerialFlowController

    ctrl = SerialFlowController(ack_timeout=1.0, response_timeout=2.0, max_attempts=3, logger=logging.getLogger("test"))
    # No sender registered
    await ctrl.acknowledge(0x01, 1)


@pytest.mark.asyncio
async def test_enqueue_mqtt_spool_unavailable_logs(runtime_config: RuntimeConfig, runtime_state: Any):
    from mcubridge.transport.mqtt import MqttTransport

    # No spool configured
    transport = MqttTransport(runtime_config, runtime_state)
    msg = structures.QueuedPublish(topic_name="test", payload=b"data")
    await transport.enqueue_mqtt(msg)
    assert runtime_state.mqtt_publish_queue.qsize() == 1


@pytest.mark.asyncio
async def test_bridge_service_publish_snapshot(runtime_config: RuntimeConfig, runtime_state: Any) -> None:
    mqtt = MagicMock()
    mqtt.publish = AsyncMock()
    service = BridgeService(runtime_config, runtime_state, mqtt)

    await service._publish_bridge_snapshot("summary", None)  # type: ignore[reportPrivateUsage]
    mqtt.publish.assert_called()


@pytest.mark.asyncio
async def test_bridge_service_reject_topic_action(runtime_config: RuntimeConfig, runtime_state: Any) -> None:
    mqtt = MagicMock()
    mqtt.publish = AsyncMock()
    service = BridgeService(runtime_config, runtime_state, mqtt)

    from aiomqtt.message import Message

    msg = Message("test", b"", 0, False, False, None)

    await service._reject_topic_action(msg, Topic.DIGITAL, "write")  # type: ignore[reportPrivateUsage]
    mqtt.publish.assert_called()


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
