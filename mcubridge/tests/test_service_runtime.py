"""Unit tests for BridgeService and related runtime orchestration (SIL-2)."""

from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, MagicMock

import msgspec
import pytest

from mcubridge.config.settings import RuntimeConfig
from mcubridge.protocol import structures
from mcubridge.protocol.protocol import Command, Status, Topic
from mcubridge.services.runtime import BridgeService
from mcubridge.state.context import create_runtime_state, RuntimeState
from mcubridge.mqtt.queue import enqueue_mqtt, QueuedPublish


def _make_config() -> RuntimeConfig:
    import os
    import time

    fs_root = f".tmp_tests/mcubridge-test-fs-{os.getpid()}-{time.time_ns()}"
    spool_dir = f".tmp_tests/mcubridge-test-spool-{os.getpid()}-{time.time_ns()}"
    return RuntimeConfig(
        serial_port="/dev/null",
        mqtt_topic="br",
        allowed_commands=("*",),
        serial_shared_secret=b"secret1234",
        file_system_root=fs_root,
        mqtt_spool_dir=spool_dir,
        allow_non_tmp_paths=True,
    )


@pytest.mark.asyncio
async def test_serial_flow_acknowledge_sends_ack_packet() -> None:
    config = _make_config()
    state = create_runtime_state(config)
    try:
        sent_frames: list[tuple[int, bytes]] = []

        async def fake_sender(
            command_id: int, payload: bytes, seq_id: int | None = None
        ) -> bool:
            sent_frames.append((command_id, payload))
            return True

        service = BridgeService(config, state, AsyncMock())
        service.serial_flow.set_sender(fake_sender)

        await service.serial_flow.acknowledge(Command.CMD_DIGITAL_WRITE.value, 123)

        assert sent_frames
        cmd_id, payload = sent_frames[0]
        assert cmd_id == Status.ACK.value
        ack = msgspec.msgpack.decode(payload, type=structures.AckPacket)
        assert ack.command_id == Command.CMD_DIGITAL_WRITE.value
    finally:
        state.cleanup()


@pytest.mark.asyncio
async def test_enqueue_mqtt_applies_reply_context_properties() -> None:
    config = _make_config()
    state = create_runtime_state(config)
    try:
        from aiomqtt.message import Message

        # Mock incoming message with ResponseTopic and CorrelationData
        from paho.mqtt.packettypes import PacketTypes
        from paho.mqtt.properties import Properties

        props = Properties(PacketTypes.PUBLISH)
        props.ResponseTopic = "reply/here"
        props.CorrelationData = b"corr-123"

        inbound = MagicMock(spec=Message)
        inbound.topic = "request/topic"
        inbound.properties = props

        msg = QueuedPublish(topic_name="original/topic", payload=b"resp")

        await enqueue_mqtt(state, msg, reply_context=inbound)

        queued = state.mqtt_publish_queue.get_nowait()
        assert queued.topic_name == "reply/here"
        assert queued.correlation_data == b"corr-123"
        # Check user properties for origin topic
        assert any(
            k == "bridge-request-topic" and _ == "request/topic"
            for k, _ in queued.user_properties
        )
    finally:
        state.cleanup()


@pytest.mark.asyncio
async def test_handle_get_version_resp_publishes_and_sets_state() -> None:
    config = _make_config()
    state = create_runtime_state(config)
    try:
        enqueue_mqtt = AsyncMock()
        service = BridgeService(config, state, enqueue_mqtt)

        pkt = structures.VersionResponsePacket(major=1, minor=2, patch=0)
        system = service.system
        await system.handle_get_version_resp(0, msgspec.msgpack.encode(pkt))

        assert state.mcu_version == (1, 2, 0)
        enqueue_mqtt.assert_called()
        msg = enqueue_mqtt.call_args.args[0]
        assert msg.payload == b"1.2.0"
    finally:
        state.cleanup()


@pytest.mark.asyncio
async def test_reject_topic_action_enqueues_status() -> None:
    config = _make_config()
    state = create_runtime_state(config)
    try:
        enqueue_mqtt = AsyncMock()
        service = BridgeService(config, state, enqueue_mqtt)

        from aiomqtt.message import Message

        mock_inbound = MagicMock(spec=Message)
        mock_inbound.topic = "br/system/reboot"
        mock_inbound.properties = None

        await service._reject_topic_action(mock_inbound, Topic.SYSTEM, "reboot")  # type: ignore[reportPrivateUsage]

        enqueue_mqtt.assert_called()
        msg = enqueue_mqtt.call_args.args[0]
        assert "response" in msg.topic_name
        assert any(k == "bridge-error" for k, _ in msg.user_properties)
    finally:
        state.cleanup()


@pytest.mark.asyncio
async def test_publish_bridge_snapshot_handshake_flavor() -> None:
    config = _make_config()
    state = create_runtime_state(config)
    try:
        enqueue_mqtt = AsyncMock()
        service = BridgeService(config, state, enqueue_mqtt)

        from aiomqtt.message import Message

        mock_inbound = MagicMock(spec=Message)
        mock_inbound.topic = "br/system/bridge/handshake/get"
        mock_inbound.properties = None

        await service._publish_bridge_snapshot("handshake", mock_inbound)  # type: ignore[reportPrivateUsage]

        enqueue_mqtt.assert_called()
        msg = enqueue_mqtt.call_args.args[0]
        assert "handshake" in msg.topic_name
    finally:
        state.cleanup()


@pytest.mark.asyncio
async def test_enqueue_mqtt_queue_full_drops_and_spools(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config = _make_config()
    state = create_runtime_state(config)
    try:
        state.mqtt_queue_limit = 1
        state.mqtt_publish_queue = asyncio.Queue(maxsize=1)

        async def _stash_ok(_self: RuntimeState, _message: QueuedPublish) -> bool:
            return True

        monkeypatch.setattr("mcubridge.mqtt.queue.stash_mqtt_message", _stash_ok)

        first = QueuedPublish(topic_name="br/old", payload=b"1")
        state.mqtt_publish_queue.put_nowait(first)

        second = QueuedPublish(topic_name="br/new", payload=b"2")

        await enqueue_mqtt(state, second)

        queued = state.mqtt_publish_queue.get_nowait()
        # Oldest was dropped, newest is in queue
        assert queued.topic_name == "br/new"
    finally:
        state.cleanup()


def test_is_topic_action_allowed_empty_action_false() -> None:
    config = _make_config()
    state = create_runtime_state(config)
    try:
        service = BridgeService(config, state, AsyncMock())
        # Default policy should DENY empty actions for safety
        assert service._is_topic_action_allowed(Topic.SYSTEM, "") is False  # type: ignore[reportPrivateUsage]
    finally:
        state.cleanup()


@pytest.mark.asyncio
async def test_enqueue_mqtt_spool_unavailable_logs(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config = _make_config()
    state = create_runtime_state(config)
    try:
        state.mqtt_queue_limit = 1
        state.mqtt_publish_queue = asyncio.Queue(maxsize=1)

        async def _stash_fail(_self: RuntimeState, _message: QueuedPublish) -> bool:
            return False

        monkeypatch.setattr("mcubridge.mqtt.queue.stash_mqtt_message", _stash_fail)

        state.mqtt_publish_queue.put_nowait(
            QueuedPublish(topic_name="br/old", payload=b"1")
        )

        await enqueue_mqtt(state, QueuedPublish(topic_name="br/new", payload=b"2"))

        queued = state.mqtt_publish_queue.get_nowait()
        assert queued.topic_name == "br/new"
    finally:
        state.cleanup()
