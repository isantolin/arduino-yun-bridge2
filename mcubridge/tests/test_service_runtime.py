"""Unit tests for BridgeService and related dispatcher logic (SIL-2)."""

from __future__ import annotations

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from mcubridge.config.settings import RuntimeConfig
from mcubridge.protocol import protocol
from mcubridge.protocol.protocol import (
    DEFAULT_BAUDRATE,
    DEFAULT_SAFE_BAUDRATE,
    Command,
    Status,
    Topic,
)
from mcubridge.services.runtime import BridgeService
from mcubridge.state.context import create_runtime_state
from mcubridge.transport.mqtt import MqttTransport


def _make_config() -> RuntimeConfig:
    return RuntimeConfig(
        serial_port="/dev/null",
        serial_baud=DEFAULT_BAUDRATE,
        serial_safe_baud=DEFAULT_SAFE_BAUDRATE,
        mqtt_host="localhost",
        mqtt_topic=protocol.MQTT_DEFAULT_TOPIC_PREFIX,
        serial_shared_secret=b"s_e_c_r_e_t_mock",
    )


@pytest.mark.asyncio
async def test_send_frame_without_serial_sender_returns_false() -> None:
    config = _make_config()
    state = create_runtime_state(config)
    try:
        service = BridgeService(config, state, MqttTransport(config, state))
        # serial_flow.send now returns False if sender is not registered
        assert await service.serial_flow.send(0x01, b"") is False
    finally:
        state.cleanup()


@pytest.mark.asyncio
async def test_schedule_background_requires_context() -> None:
    config = _make_config()
    state = create_runtime_state(config)
    try:
        service = BridgeService(config, state, MqttTransport(config, state))
        coro = asyncio.sleep(0)
        with pytest.raises(RuntimeError, match="not entered"):
            await service.schedule_background(coro)
        await coro
    finally:
        state.cleanup()


@pytest.mark.asyncio
async def testacknowledge_mcu_frame_no_sender_is_noop() -> None:
    config = _make_config()
    state = create_runtime_state(config)
    try:
        service = BridgeService(config, state, MqttTransport(config, state))
        # acknowledge returns False when no sender
        assert await service.serial_flow.acknowledge(0x01, 1) is False
    finally:
        state.cleanup()


@pytest.mark.asyncio
async def testacknowledge_mcu_frame_sends_ack_packet() -> None:
    config = _make_config()
    state = create_runtime_state(config)
    try:
        service = BridgeService(config, state, MqttTransport(config, state))
        mock_sender = AsyncMock(return_value=True)
        service.register_serial_sender(mock_sender)

        await service.serial_flow.acknowledge(0x42, 123)

        mock_sender.assert_called_once()
        args = mock_sender.call_args.args
        assert args[0] == Status.ACK.value
        assert args[2] == 123
    finally:
        state.cleanup()


@pytest.mark.asyncio
async def test_reject_topic_action_enqueues_status() -> None:
    config = _make_config()
    state = create_runtime_state(config)
    try:
        mqtt_mock = MagicMock(spec=MqttTransport)
        mqtt_mock.reject_topic_action = AsyncMock()
        service = BridgeService(config, state, mqtt_mock)

        inbound = SimpleNamespace(
            topic=f"{protocol.MQTT_DEFAULT_TOPIC_PREFIX}/system/secret",
            properties=None,
        )
        await service.mqtt_flow.reject_topic_action(inbound, Topic.SYSTEM, "reboot")
        mqtt_mock.reject_topic_action.assert_called_with(inbound, Topic.SYSTEM, "reboot")
    finally:
        state.cleanup()


@pytest.mark.asyncio
async def test_publish_bridge_snapshot_handshake_flavor() -> None:
    config = _make_config()
    state = create_runtime_state(config)
    try:
        mqtt_mock = MagicMock(spec=MqttTransport)
        mqtt_mock.publish_bridge_snapshot = AsyncMock()
        service = BridgeService(config, state, mqtt_mock)

        inbound = SimpleNamespace(
            topic=f"{protocol.MQTT_DEFAULT_TOPIC_PREFIX}/system/bridge/handshake/get",
            properties=None,
        )
        await service.mqtt_flow.publish_bridge_snapshot("handshake", inbound)
        mqtt_mock.publish_bridge_snapshot.assert_called_with("handshake", inbound)
    finally:
        state.cleanup()


def test_is_topic_action_allowed_empty_action_true() -> None:
    config = _make_config()
    state = create_runtime_state(config)
    try:
        mqtt_mock = MagicMock(spec=MqttTransport)
        mqtt_mock.is_topic_action_allowed = MagicMock(return_value=True)
        service = BridgeService(config, state, mqtt_mock)

        assert service.mqtt_flow.is_topic_action_allowed(Topic.SYSTEM, "") is True
    finally:
        state.cleanup()


@pytest.mark.asyncio
async def test_handle_get_free_memory_resp_malformed_no_publish() -> None:
    config = _make_config()
    state = create_runtime_state(config)
    try:
        service = BridgeService(config, state, MqttTransport(config, state))
        # Empty payload is malformed for FreeMemoryResponsePacket
        await service.handle_mcu_frame(Command.CMD_GET_FREE_MEMORY_RESP.value, 0, b"")
        assert state.mqtt_publish_queue.qsize() == 0
    finally:
        state.cleanup()


@pytest.mark.asyncio
async def test_handle_get_version_resp_publishes_and_sets_state() -> None:
    config = _make_config()
    state = create_runtime_state(config)
    try:
        service = BridgeService(config, state, MqttTransport(config, state))
        from mcubridge.protocol.structures import VersionResponsePacket

        payload = VersionResponsePacket(major=1, minor=2, patch=3).encode()
        await service.handle_mcu_frame(Command.CMD_GET_VERSION_RESP.value, 0, payload)

        assert state.mcu_version == (1, 2, 3)
        assert state.mqtt_publish_queue.qsize() > 0
    finally:
        state.cleanup()


@pytest.mark.asyncio
async def test_enqueue_mqtt_applies_reply_context_properties() -> None:
    config = _make_config()
    state = create_runtime_state(config)
    try:
        # We need real MqttTransport to test logic
        transport = MqttTransport(config, state)
        service = BridgeService(config, state, transport)

        inbound = SimpleNamespace(
            topic="reply/to/me",
            properties=SimpleNamespace(ResponseTopic="resp", CorrelationData=b"cid"),
        )

        await service.mqtt_flow.publish(
            topic="some/topic",
            payload=b"p",
            reply_to=inbound,
        )

        queued = state.mqtt_publish_queue.get_nowait()
        assert queued.response_topic == "resp"
        assert queued.correlation_data == b"cid"
    finally:
        state.cleanup()


@pytest.mark.asyncio
async def test_enqueue_mqtt_queue_full_drops_and_spools(tmp_path: Any) -> None:
    config = _make_config()
    config.mqtt_queue_limit = 1
    # Use tmp_path for spool to avoid sqlite3 errors
    config.mqtt_spool_dir = str(tmp_path / "spool")
    state = create_runtime_state(config, initialize_spool=True)
    try:
        from mcubridge.protocol.structures import QueuedPublish
        transport = MqttTransport(config, state)
        # Block the queue with one message
        state.mqtt_publish_queue.put_nowait(QueuedPublish(topic_name="block", payload=b"b"))

        await transport.publish("dropped", b"p")

        assert state.mqtt_dropped_messages > 0
        # If spool is enabled, it should have been spooled
        assert state.mqtt_spooled_messages > 0
    finally:
        state.cleanup()


@pytest.mark.asyncio
async def test_enqueue_mqtt_spool_unavailable_logs(tmp_path: Any) -> None:
    config = _make_config()
    config.mqtt_queue_limit = 1
    config.mqtt_spool_dir = str(tmp_path / "spool_none")
    state = create_runtime_state(config, initialize_spool=False)
    try:
        from mcubridge.protocol.structures import QueuedPublish
        transport = MqttTransport(config, state)
        state.mqtt_publish_queue.put_nowait(QueuedPublish(topic_name="block", payload=b"b"))

        # Should not raise even if spool is None
        await transport.publish("dropped", b"p")
        assert state.mqtt_dropped_messages > 0
    finally:
        state.cleanup()
