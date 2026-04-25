"""Focused unit tests for BridgeService (runtime)."""

from __future__ import annotations

import asyncio
import time
from unittest.mock import AsyncMock, MagicMock, patch

import msgspec
import pytest
from mcubridge.config.settings import RuntimeConfig
from mcubridge.protocol.structures import QueuedPublish
from mcubridge.protocol import protocol, structures
from mcubridge.protocol.protocol import Status
from mcubridge.protocol.topics import Topic, topic_path
from mcubridge.services.runtime import BridgeService
from mcubridge.services.system import SystemComponent
from mcubridge.state.context import RuntimeState, create_runtime_state
from mcubridge.mqtt.spool_manager import MqttSpoolManager

from tests._helpers import make_test_config


def _make_config() -> RuntimeConfig:
    return make_test_config(
        allowed_commands=("echo", "ls"),
        serial_shared_secret=b"testshared",
    )


@pytest.mark.asyncio
async def test_send_frame_without_serial_sender_returns_false() -> None:
    config = _make_config()
    state = create_runtime_state(config)
    try:
        spool = MagicMock(spec=MqttSpoolManager)
        service = BridgeService(config, state, spool)

        ok = await service.serial_flow.send(
            protocol.Command.CMD_GET_VERSION.value, b"x"
        )
        assert ok is False
    finally:
        state.cleanup()


@pytest.mark.asyncio
async def test_serial_flow_acknowledge_no_sender_is_noop() -> None:
    config = _make_config()
    state = create_runtime_state(config)
    try:
        spool = MagicMock(spec=MqttSpoolManager)
        service = BridgeService(config, state, spool)

        await service.serial_flow.acknowledge(
            protocol.Command.CMD_GET_VERSION.value, 0, status=Status.ACK
        )
    finally:
        state.cleanup()


@pytest.mark.asyncio
async def test_serial_flow_acknowledge_sends_ack_packet() -> None:
    config = _make_config()
    state = create_runtime_state(config)
    try:
        spool = MagicMock(spec=MqttSpoolManager)
        service = BridgeService(config, state, spool)

        sent: list[tuple[int, bytes]] = []

        async def _sender_side_effect(
            cmd: int, payload: bytes, seq_id: int | None = None
        ) -> bool:
            sent.append((cmd, payload))
            return True

        mock_sender = AsyncMock(side_effect=_sender_side_effect)
        service.register_serial_sender(mock_sender)

        await service.serial_flow.acknowledge(
            protocol.Command.CMD_GET_FREE_MEMORY.value,
            0,
            status=Status.MALFORMED,
        )

        assert sent
        status_cmd, payload = sent[0]
        assert status_cmd == Status.MALFORMED.value
        assert payload == msgspec.msgpack.encode(
            structures.AckPacket(command_id=protocol.Command.CMD_GET_FREE_MEMORY.value)
        )
    finally:
        state.cleanup()


@pytest.mark.asyncio
async def test_enqueue_mqtt_applies_reply_context_properties() -> None:
    config = _make_config()
    state = create_runtime_state(config)
    try:
        spool = MagicMock(spec=MqttSpoolManager)
        service = BridgeService(config, state, spool)
        
        msg = QueuedPublish(
            topic_name=f"{protocol.MQTT_DEFAULT_TOPIC_PREFIX}/x", payload=b"hello"
        )

        from aiomqtt.message import Message
        mock_props = MagicMock()
        mock_props.ResponseTopic = f"{protocol.MQTT_DEFAULT_TOPIC_PREFIX}/resp"
        mock_props.CorrelationData = b"cid"

        mock_inbound = MagicMock(spec=Message)
        mock_inbound.topic = f"{protocol.MQTT_DEFAULT_TOPIC_PREFIX}/origin"
        mock_inbound.properties = mock_props

        await service.enqueue_mqtt(msg, reply_context=mock_inbound)

        queued = state.mqtt_publish_queue.get_nowait()
        assert queued.topic_name == f"{protocol.MQTT_DEFAULT_TOPIC_PREFIX}/resp"
        assert queued.correlation_data == b"cid"
        assert any(k == "bridge-request-topic" for k, v in queued.user_properties)
    finally:
        state.cleanup()


@pytest.mark.asyncio
async def test_enqueue_mqtt_queue_full_drops_and_spools() -> None:
    config = _make_config()
    state = create_runtime_state(config)
    try:
        state.mqtt_queue_limit = 1
        state.mqtt_publish_queue = asyncio.Queue(maxsize=1)

        spool = MagicMock(spec=MqttSpoolManager)
        spool.stash = AsyncMock(return_value=True)
        service = BridgeService(config, state, spool)

        first = QueuedPublish(topic_name="old", payload=b"1")
        state.mqtt_publish_queue.put_nowait(first)

        second = QueuedPublish(topic_name="new", payload=b"2")
        await service.enqueue_mqtt(second)

        # Queue now contains the new message.
        queued = state.mqtt_publish_queue.get_nowait()
        assert queued.topic_name == "new"

        # Stash called for the old message
        spool.stash.assert_called_once_with(first)
        assert state.mqtt_dropped_messages == 1
    finally:
        state.cleanup()


@pytest.mark.asyncio
async def test_handle_get_free_memory_resp_malformed_no_publish() -> None:
    config = _make_config()
    state = create_runtime_state(config)
    try:
        spool = MagicMock(spec=MqttSpoolManager)
        service = BridgeService(config, state, spool)

        system = service._container.get(SystemComponent)
        await system.handle_get_free_memory_resp(0, protocol.FRAME_DELIMITER)
        assert state.mqtt_publish_queue.qsize() == 0
    finally:
        state.cleanup()


@pytest.mark.asyncio
async def test_handle_get_version_resp_publishes_and_sets_state() -> None:
    config = _make_config()
    state = create_runtime_state(config)
    try:
        spool = MagicMock(spec=MqttSpoolManager)
        service = BridgeService(config, state, spool)

        pkt = structures.VersionResponsePacket(major=1, minor=2, patch=0)
        system = service._container.get(SystemComponent)
        await system.handle_get_version_resp(0, msgspec.msgpack.encode(pkt))

        assert state.mcu_version == (1, 2, 0)
        queued = state.mqtt_publish_queue.get_nowait()
        assert queued.payload == b"1.2.0"
    finally:
        state.cleanup()


@pytest.mark.asyncio
async def test_reject_topic_action_enqueues_status() -> None:
    config = _make_config()
    state = create_runtime_state(config)
    try:
        spool = MagicMock(spec=MqttSpoolManager)
        service = BridgeService(config, state, spool)

        from aiomqtt.message import Message
        mock_inbound = MagicMock(spec=Message)
        mock_inbound.topic = f"{protocol.MQTT_DEFAULT_TOPIC_PREFIX}/system/secret"
        mock_inbound.properties = None

        await service._reject_topic_action(mock_inbound, Topic.SYSTEM, "reboot")

        queued = state.mqtt_publish_queue.get_nowait()
        assert "status" in queued.topic_name
    finally:
        state.cleanup()
