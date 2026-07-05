"""Focused unit tests for BridgeService (runtime)."""

from __future__ import annotations
from mcubridge.transport.serial import SerialTransport

import time
import asyncio
from unittest.mock import AsyncMock, MagicMock
from typing import Any

import pytest
from mcubridge.config.settings import RuntimeConfig
from mcubridge.protocol import protocol
from mcubridge.protocol.structures import create_queued_publish
from mcubridge.protocol import mcubridge_pb2 as pb
from mcubridge.services.runtime import BridgeService
from mcubridge.state.context import create_runtime_state


def _make_config() -> RuntimeConfig:
    import os

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
async def test_send_frame_via_transport() -> None:
    service = None
    config = _make_config()
    state = create_runtime_state(config)
    try:
        mock_serial = AsyncMock(spec=SerialTransport)
        mock_serial.send.return_value = True
        service = BridgeService(config, state, mock_serial)

        assert service.serial is not None
        ok = await service.serial.send(protocol.Command.CMD_GET_VERSION.value, b"x")
        assert ok is True
        mock_serial.send.assert_called_once()
    finally:
        if service is not None:
            service.cleanup()
        else:
            state.cleanup()


@pytest.mark.asyncio
async def test_handle_mcu_frame_pre_sync_denied() -> None:
    service = None
    config = _make_config()
    state = create_runtime_state(config)
    try:
        mock_serial = AsyncMock(spec=SerialTransport)
        service = BridgeService(config, state, mock_serial)
        state.state = "unsynchronized"

        # CMD_GET_VERSION is not in pre-sync allowed list (64 is MIN_SYS but not sync/reset)
        await service.handle_mcu_frame(protocol.Command.CMD_GET_VERSION.value, 1, b"")
        mock_serial.acknowledge.assert_not_called()
    finally:
        if service is not None:
            service.cleanup()
        else:
            state.cleanup()


@pytest.mark.asyncio
async def test_handle_mcu_xon_xoff() -> None:
    service = None
    config = _make_config()
    state = create_runtime_state(config)
    try:
        service = BridgeService(
            config,
            state,
            AsyncMock(spec=SerialTransport),
        )
        state.state = "synchronized"

        await service.handle_mcu_frame(protocol.Command.CMD_XOFF.value, 1, b"")
        assert state.mcu_is_paused is True
        assert state.serial_tx_allowed.is_set() is False

        await service.handle_mcu_frame(protocol.Command.CMD_XON.value, 2, b"")
        assert state.mcu_is_paused is False
        assert state.serial_tx_allowed.is_set() is True
    finally:
        if service is not None:
            service.cleanup()
        else:
            state.cleanup()


@pytest.mark.asyncio
async def test_handle_mqtt_console_queues_and_flushes() -> None:
    service = None
    config = _make_config()
    state = create_runtime_state(config)
    try:
        mock_serial = AsyncMock(spec=SerialTransport)
        mock_serial.send.return_value = True
        service = BridgeService(config, state, mock_serial)
        state.state = "synchronized"
        state.link_sync_event.set()
        state.serial_tx_allowed.set()

        from aiomqtt import PublishPacket

        mock_msg = AsyncMock(spec=PublishPacket)
        mock_msg.topic = "br/console/in"
        mock_msg.payload = b"hello"

        await service.handle_request(mock_msg)

        mock_serial.send.assert_called()
    finally:
        if service is not None:
            service.cleanup()
        else:
            state.cleanup()


@pytest.mark.asyncio
async def test_enqueue_mqtt_spools_until_client_recovers() -> None:
    service = None
    config = _make_config()
    state = create_runtime_state(config)
    try:
        service = BridgeService(config, state, AsyncMock(spec=SerialTransport))
        message = create_queued_publish("br/system/status", b"payload")

        await service.enqueue_mqtt(message)

        assert state.mqtt_spool_pending_messages == 1

        mock_client = AsyncMock()
        service.set_mqtt_client(mock_client)
        await service.flush_mqtt_spool()

        mock_client.publish.assert_awaited_once()
        assert state.mqtt_spool_pending_messages == 0
    finally:
        if service is not None:
            service.cleanup()
        else:
            state.cleanup()


@pytest.mark.asyncio
async def test_handle_mqtt_pin_overflow_reports_error() -> None:
    service = None
    config = _make_config()
    state = create_runtime_state(config)
    try:
        from unittest.mock import patch
        from mcubridge.protocol.structures import PendingPinRequest

        mock_serial = AsyncMock(spec=SerialTransport)
        service = BridgeService(config, state, mock_serial)
        state.state = "synchronized"
        state.link_sync_event.set()
        state.pending_pin_request_limit = 1
        state.pending_digital_reads.append(PendingPinRequest(pin=13, reply_context=None))

        captured: list[pb.MqttQueuedPublish] = []

        async def capture_enqueue(message: pb.MqttQueuedPublish, *, reply_context: object | None = None) -> None:
            del reply_context
            captured.append(message)

        with patch.object(service, "enqueue_mqtt", side_effect=capture_enqueue):
            from aiomqtt import PublishPacket

            message = AsyncMock(spec=PublishPacket)
            message.topic = "br/d/13/read"
            message.payload = b""
            message.properties = None

            await service.handle_request(message)

        assert captured
        assert any(
            prop.key == "bridge-error" and prop.value == "pending-pin-overflow" for prop in captured[0].user_properties
        )
        mock_serial.send.assert_not_called()
    finally:
        if service is not None:
            service.cleanup()
        else:
            state.cleanup()


@pytest.mark.asyncio
async def test_mqtt_topic_aliases() -> None:
    service = None
    config = _make_config()
    state = create_runtime_state(config)
    try:
        mock_serial = AsyncMock(spec=SerialTransport)
        service = BridgeService(config, state, mock_serial)

        # 1. Setup mock client with topic_alias_max = 2
        mock_client = AsyncMock()
        mock_connack = MagicMock()
        mock_connack.topic_alias_max = 2
        mock_connected: asyncio.Future[Any] = asyncio.Future()
        mock_connected.set_result(mock_connack)
        mock_client._connected = mock_connected

        service.set_mqtt_client(mock_client)

        # Publish Topic A (first time)
        msg_a1 = create_queued_publish(topic_name="topic/A", payload=b"payload_a1")
        await service.enqueue_mqtt(msg_a1)

        # Publish Topic A (second time)
        msg_a2 = create_queued_publish(topic_name="topic/A", payload=b"payload_a2")
        await service.enqueue_mqtt(msg_a2)

        # Publish Topic B (first time)
        msg_b1 = create_queued_publish(topic_name="topic/B", payload=b"payload_b1")
        await service.enqueue_mqtt(msg_b1)

        # Publish Topic B (second time)
        msg_b2 = create_queued_publish(topic_name="topic/B", payload=b"payload_b2")
        await service.enqueue_mqtt(msg_b2)

        # Verify publish calls
        assert mock_client.publish.call_count == 4

        # Call 1: topic/A with alias 1
        args_1, kwargs_1 = mock_client.publish.call_args_list[0]
        assert args_1[0] == "topic/A"
        assert args_1[1] == b"payload_a1"
        assert kwargs_1.get("topic_alias") == 1

        # Call 2: empty topic with alias 1
        args_2, kwargs_2 = mock_client.publish.call_args_list[1]
        assert args_2[0] == ""
        assert args_2[1] == b"payload_a2"
        assert kwargs_2.get("topic_alias") == 1

        # Call 3: topic/B with alias 2
        args_3, kwargs_3 = mock_client.publish.call_args_list[2]
        assert args_3[0] == "topic/B"
        assert args_3[1] == b"payload_b1"
        assert kwargs_3.get("topic_alias") == 2

        # Call 4: empty topic with alias 2
        args_4, kwargs_4 = mock_client.publish.call_args_list[3]
        assert args_4[0] == ""
        assert args_4[1] == b"payload_b2"
        assert kwargs_4.get("topic_alias") == 2

        # 2. Test Reset-on-reconnect behavior
        mock_client_new = AsyncMock()
        mock_connack_new = MagicMock()
        mock_connack_new.topic_alias_max = 2
        mock_connected_new: asyncio.Future[Any] = asyncio.Future()
        mock_connected_new.set_result(mock_connack_new)
        mock_client_new._connected = mock_connected_new

        service.set_mqtt_client(mock_client_new)

        # Publish Topic A (first time after reset)
        await service.enqueue_mqtt(msg_a1)

        assert mock_client_new.publish.call_count == 1
        args_new, kwargs_new = mock_client_new.publish.call_args_list[0]
        # Should have full topic and alias 1 again
        assert args_new[0] == "topic/A"
        assert kwargs_new.get("topic_alias") == 1
    finally:
        if service is not None:
            service.cleanup()
        else:
            state.cleanup()


@pytest.mark.asyncio
async def test_mqtt_topic_aliases_limit_boundary() -> None:
    service = None
    config = _make_config()
    state = create_runtime_state(config)
    try:
        mock_serial = AsyncMock(spec=SerialTransport)
        service = BridgeService(config, state, mock_serial)

        # Setup mock client with topic_alias_max = 1
        mock_client = AsyncMock()
        mock_connack = MagicMock()
        mock_connack.topic_alias_max = 1
        mock_connected: asyncio.Future[Any] = asyncio.Future()
        mock_connected.set_result(mock_connack)
        mock_client._connected = mock_connected
        service.set_mqtt_client(mock_client)

        # Topic A -> mapped to alias 1
        await service.enqueue_mqtt(create_queued_publish(topic_name="topic/A", payload=b"a1"))
        # Topic B -> no room, published normally (no alias)
        await service.enqueue_mqtt(create_queued_publish(topic_name="topic/B", payload=b"b1"))
        # Topic A again -> alias 1 (empty topic)
        await service.enqueue_mqtt(create_queued_publish(topic_name="topic/A", payload=b"a2"))
        # Topic B again -> no room, published normally (no alias)
        await service.enqueue_mqtt(create_queued_publish(topic_name="topic/B", payload=b"b2"))

        assert mock_client.publish.call_count == 4

        # Topic A 1st: full topic, alias 1
        args_1, kwargs_1 = mock_client.publish.call_args_list[0]
        assert args_1[0] == "topic/A"
        assert kwargs_1.get("topic_alias") == 1

        # Topic B 1st: full topic, no alias
        args_2, kwargs_2 = mock_client.publish.call_args_list[1]
        assert args_2[0] == "topic/B"
        assert kwargs_2.get("topic_alias") is None

        # Topic A 2nd: empty topic, alias 1
        args_3, kwargs_3 = mock_client.publish.call_args_list[2]
        assert args_3[0] == ""
        assert kwargs_3.get("topic_alias") == 1

        # Topic B 2nd: full topic, no alias
        args_4, kwargs_4 = mock_client.publish.call_args_list[3]
        assert args_4[0] == "topic/B"
        assert kwargs_4.get("topic_alias") is None
    finally:
        if service is not None:
            service.cleanup()
        else:
            state.cleanup()


@pytest.mark.asyncio
async def test_mqtt_topic_aliases_disabled() -> None:
    service = None
    config = _make_config()
    state = create_runtime_state(config)
    try:
        mock_serial = AsyncMock(spec=SerialTransport)
        service = BridgeService(config, state, mock_serial)

        # Setup mock client with topic_alias_max = 0 (or missing)
        mock_client = AsyncMock()
        mock_connack = MagicMock()
        mock_connack.topic_alias_max = 0
        mock_connected: asyncio.Future[Any] = asyncio.Future()
        mock_connected.set_result(mock_connack)
        mock_client._connected = mock_connected
        service.set_mqtt_client(mock_client)

        await service.enqueue_mqtt(create_queued_publish(topic_name="topic/A", payload=b"a1"))
        await service.enqueue_mqtt(create_queued_publish(topic_name="topic/A", payload=b"a2"))

        assert mock_client.publish.call_count == 2

        # Both should have full topic and no TopicAlias property set
        args_1, kwargs_1 = mock_client.publish.call_args_list[0]
        assert args_1[0] == "topic/A"
        assert kwargs_1.get("topic_alias") is None

        args_2, kwargs_2 = mock_client.publish.call_args_list[1]
        assert args_2[0] == "topic/A"
        assert kwargs_2.get("topic_alias") is None
    finally:
        if service is not None:
            service.cleanup()
        else:
            state.cleanup()
