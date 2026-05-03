"""Focused unit tests for BridgeService (runtime)."""

from __future__ import annotations
import msgspec
from mcubridge.transport.mqtt import MqttTransport

import asyncio
import time
from unittest.mock import AsyncMock

import pytest
from mcubridge.config.settings import RuntimeConfig
from mcubridge.protocol.structures import QueuedPublish
from mcubridge.protocol import protocol, structures
from mcubridge.protocol.protocol import Status
from mcubridge.protocol.topics import Topic, topic_path
from mcubridge.services.runtime import BridgeService
from mcubridge.state.context import RuntimeState, create_runtime_state


def _make_config() -> RuntimeConfig:
    import os
    import time

    fs_root = f".tmp_tests/mcubridge-test-fs-{os.getpid()}-{time.time_ns()}"
    spool_dir = f".tmp_tests/mcubridge-test-spool-{os.getpid()}-{time.time_ns()}"
    return RuntimeConfig(
        allowed_commands=("echo", "ls"),
        serial_shared_secret=b"testshared",
        file_system_root=fs_root,
        mqtt_spool_dir=spool_dir,
        allow_non_tmp_paths=True,
    )


@pytest.mark.asyncio
async def test_send_frame_without_serial_sender_returns_false() -> None:
    config = _make_config()
    state = create_runtime_state(config)
    try:
        service = BridgeService(config, state, MqttTransport(config, state))

        # Testing direct serial flow via service property
        ok = await service.serial_flow.send(
            protocol.Command.CMD_GET_VERSION.value, b"x"
        )
        assert ok is False
    finally:
        state.cleanup()


@pytest.mark.asyncio
async def test_schedule_background_requires_context() -> None:
    config = _make_config()
    state = create_runtime_state(config)
    try:
        service = BridgeService(config, state, MqttTransport(config, state))

        async def _coro() -> None:
            return None

        pending = _coro()
        try:
            with pytest.raises(RuntimeError):
                await service.schedule_background(pending)
        finally:
            pending.close()
    finally:
        state.cleanup()


@pytest.mark.asyncio
async def test_serial_flow_acknowledge_no_sender_is_noop() -> None:
    config = _make_config()
    state = create_runtime_state(config)
    try:
        service = BridgeService(config, state, MqttTransport(config, state))

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
        service = BridgeService(config, state, MqttTransport(config, state))

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
        msg = QueuedPublish(
            topic_name=f"{protocol.MQTT_DEFAULT_TOPIC_PREFIX}/x", payload=b"hello"
        )

        # [SIL-2] Use AsyncMock(spec=...) for properties and inbound message
        from aiomqtt.message import Message

        mock_props = AsyncMock()
        mock_props.ResponseTopic = f"{protocol.MQTT_DEFAULT_TOPIC_PREFIX}/resp"
        mock_props.CorrelationData = b"cid"

        mock_inbound = AsyncMock(spec=Message)
        mock_inbound.topic = f"{protocol.MQTT_DEFAULT_TOPIC_PREFIX}/origin"
        mock_inbound.properties = mock_props

        # Calling direct state method
        await MqttTransport(config, state).enqueue_mqtt(msg, reply_context=mock_inbound)

        queued = state.mqtt_publish_queue.get_nowait()
        assert queued.topic_name == f"{protocol.MQTT_DEFAULT_TOPIC_PREFIX}/resp"
        assert queued.correlation_data == b"cid"
        assert (
            "bridge-request-topic",
            f"{protocol.MQTT_DEFAULT_TOPIC_PREFIX}/origin",
        ) in queued.user_properties
    finally:
        state.cleanup()


@pytest.mark.asyncio
async def test_enqueue_mqtt_queue_full_drops_and_spools(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config = _make_config()
    state = create_runtime_state(config)
    try:
        # Create a tiny queue and fill it.
        state.mqtt_queue_limit = 1
        state.mqtt_publish_queue = asyncio.Queue(maxsize=1)

        # Avoid touching the real spool implementation (RuntimeState is slots=True,
        # so patch the class method rather than the instance attribute).
        async def _stash_ok(_self: RuntimeState, _message: QueuedPublish) -> bool:
            return True

        monkeypatch.setattr(MqttTransport, "stash_mqtt_message", _stash_ok)

        from mcubridge.mqtt.spool import MQTTPublishSpool

        # [SIL-2] Use spec=MQTTPublishSpool for high fidelity
        mock_spool = AsyncMock(spec=MQTTPublishSpool)
        mock_spool.pending = 3
        state.mqtt_spool = mock_spool

        first = QueuedPublish(
            topic_name=f"{protocol.MQTT_DEFAULT_TOPIC_PREFIX}/old",
            payload=b"1",
        )
        state.mqtt_publish_queue.put_nowait(first)

        second = QueuedPublish(
            topic_name=f"{protocol.MQTT_DEFAULT_TOPIC_PREFIX}/new",
            payload=b"2",
        )
        # Calling direct state method
        await MqttTransport(config, state).enqueue_mqtt(second)

        # Queue now contains the new message.
        queued = state.mqtt_publish_queue.get_nowait()
        assert queued.topic_name == f"{protocol.MQTT_DEFAULT_TOPIC_PREFIX}/new"

        # Drop counters updated.
        assert state.mqtt_dropped_messages == 1
    finally:
        state.cleanup()


@pytest.mark.asyncio
async def test_handle_get_free_memory_resp_malformed_no_publish() -> None:
    config = _make_config()
    state = create_runtime_state(config)
    try:
        service = BridgeService(config, state, MqttTransport(config, state))

        system = service.system
        await system.handle_get_free_memory_resp(0, protocol.FRAME_DELIMITER)
        assert state.mqtt_publish_queue.qsize() == 0
    finally:
        state.cleanup()


@pytest.mark.asyncio
async def test_handle_get_version_resp_publishes_and_sets_state() -> None:
    config = _make_config()
    state = create_runtime_state(config)
    try:
        service = BridgeService(config, state, MqttTransport(config, state))

        pkt = structures.VersionResponsePacket(major=1, minor=2, patch=0)
        system = service.system
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
        service = BridgeService(config, state, MqttTransport(config, state))

        from aiomqtt.message import Message

        # [SIL-2] Use spec=Message
        mock_inbound = AsyncMock(spec=Message)
        mock_inbound.topic = f"{protocol.MQTT_DEFAULT_TOPIC_PREFIX}/system/secret"
        mock_inbound.properties = None

        await service._reject_topic_action(mock_inbound, Topic.SYSTEM, "reboot")  # type: ignore[reportPrivateUsage]

        queued = state.mqtt_publish_queue.get_nowait()
        status_topic = topic_path(state.mqtt_topic_prefix, Topic.SYSTEM, Topic.STATUS)
        assert queued.topic_name == status_topic
        body = msgspec.msgpack.decode(queued.payload)
        assert body["status"] == "forbidden"
    finally:
        state.cleanup()


@pytest.mark.asyncio
async def test_publish_bridge_snapshot_handshake_flavor() -> None:
    config = _make_config()
    state = create_runtime_state(config)
    try:
        service = BridgeService(config, state, MqttTransport(config, state))

        from aiomqtt.message import Message

        # [SIL-2] Use spec=Message
        mock_inbound = AsyncMock(spec=Message)
        mock_inbound.topic = (
            f"{protocol.MQTT_DEFAULT_TOPIC_PREFIX}/system/bridge/handshake/get"
        )
        mock_inbound.properties = None

        await service._publish_bridge_snapshot("handshake", mock_inbound)  # type: ignore[reportPrivateUsage]

        queued = state.mqtt_publish_queue.get_nowait()
        assert "bridge/handshake/value" in queued.topic_name
    finally:
        state.cleanup()


def test_is_topic_action_allowed_empty_action_true() -> None:
    config = _make_config()
    state = create_runtime_state(config)
    try:
        service = BridgeService(config, state, MqttTransport(config, state))

        assert service._is_topic_action_allowed(Topic.SYSTEM, "") is True  # type: ignore[reportPrivateUsage]
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

        monkeypatch.setattr(MqttTransport, "stash_mqtt_message", _stash_fail)

        state.mqtt_spool_failure_reason = "disabled"
        state.mqtt_spool_backoff_until = time.monotonic() + 5

        state.mqtt_publish_queue.put_nowait(
            QueuedPublish(
                topic_name=f"{protocol.MQTT_DEFAULT_TOPIC_PREFIX}/old",
                payload=b"1",
            )
        )

        await MqttTransport(config, state).enqueue_mqtt(
            QueuedPublish(
                topic_name=f"{protocol.MQTT_DEFAULT_TOPIC_PREFIX}/new",
                payload=b"2",
            )
        )

        queued = state.mqtt_publish_queue.get_nowait()
        assert queued.topic_name == f"{protocol.MQTT_DEFAULT_TOPIC_PREFIX}/new"
    finally:
        state.cleanup()
