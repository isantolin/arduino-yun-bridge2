"""Unit tests validating cloud command request/response loop and telemetry parity (SIL-2)."""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from unittest.mock import AsyncMock, MagicMock
import pytest

from mcubridge.config.settings import RuntimeConfig
import mcubridge.protocol.mcubridge_pb2 as pb
from mcubridge.protocol.protocol import Command, Topic
from mcubridge.protocol.topics import TopicRoute, parse_topic, topic_path
from mcubridge.services.runtime import BridgeService
from mcubridge.state.context import RuntimeState, create_runtime_state
from mcubridge.transport.serial import SerialTransport


def _make_config() -> RuntimeConfig:
    return RuntimeConfig(
        allowed_commands=("echo", "ls"),
        serial_shared_secret=b"testsharedsecret",
        allow_non_tmp_paths=True,
    )


@pytest.fixture
def test_config() -> RuntimeConfig:
    return _make_config()


@pytest.fixture
def mock_bridge_state(test_config: RuntimeConfig) -> RuntimeState:
    return create_runtime_state(test_config)


@pytest.mark.asyncio
async def test_cloud_command_digital_write_with_correlation(
    test_config: RuntimeConfig, mock_bridge_state: RuntimeState
) -> None:
    """Validate that cloud digital write command replies with correlated response."""
    serial_mock = AsyncMock(spec=SerialTransport)
    serial_mock.send.return_value = True

    svc = BridgeService(test_config, mock_bridge_state, serial_mock)
    svc.enqueue_cloud_publish = AsyncMock()

    t_path = topic_path(mock_bridge_state.cloud_topic_prefix, Topic.DIGITAL, "13")
    route = parse_topic(mock_bridge_state.cloud_topic_prefix, t_path)
    assert route is not None

    inbound = pb.CloudQueuedPublish(
        topic_name=t_path,
        payload=b"1",
        correlation_data=(12345).to_bytes(8, "big"),
        response_topic="cloud",
    )

    handle_pin: Callable[[TopicRoute, pb.CloudQueuedPublish], Awaitable[None]] = getattr(svc, "_handle_pin")
    await handle_pin(route, inbound)

    # Validate serial command was dispatched
    serial_mock.send.assert_awaited_once()
    args, _ = serial_mock.send.await_args
    assert args[0] == Command.CMD_DIGITAL_WRITE.value
    assert isinstance(args[1], pb.DigitalWrite)
    assert args[1].pin == 13
    assert args[1].value == 1

    # Validate cloud response was enqueued with correlation context
    svc.enqueue_cloud_publish.assert_awaited_once()
    assert svc.enqueue_cloud_publish.await_args is not None
    publish_args, publish_kwargs = svc.enqueue_cloud_publish.await_args
    assert f"{Topic.DIGITAL.value}/13/response" in publish_args[0]
    assert publish_args[1] == b"OK"
    assert publish_kwargs["reply_context"] == inbound


@pytest.mark.asyncio
async def test_cloud_command_pin_mode_with_correlation(
    test_config: RuntimeConfig, mock_bridge_state: RuntimeState
) -> None:
    """Validate that cloud pin mode command replies with correlated response."""
    serial_mock = AsyncMock(spec=SerialTransport)
    serial_mock.send.return_value = True

    svc = BridgeService(test_config, mock_bridge_state, serial_mock)
    svc.enqueue_cloud_publish = AsyncMock()

    t_path = topic_path(mock_bridge_state.cloud_topic_prefix, Topic.DIGITAL, "13", "mode")
    route = parse_topic(mock_bridge_state.cloud_topic_prefix, t_path)
    assert route is not None

    inbound = pb.CloudQueuedPublish(
        topic_name=t_path,
        payload=b"1",
        correlation_data=(67890).to_bytes(8, "big"),
        response_topic="cloud",
    )

    handle_pin_mode: Callable[[TopicRoute, pb.CloudQueuedPublish], Awaitable[None]] = getattr(svc, "_handle_pin")
    await handle_pin_mode(route, inbound)

    # Validate serial command was dispatched
    serial_mock.send.assert_awaited_once()
    args, _ = serial_mock.send.await_args
    assert args[0] == Command.CMD_SET_PIN_MODE.value
    assert isinstance(args[1], pb.PinMode)
    assert args[1].pin == 13

    # Validate cloud response was enqueued with correlation context
    svc.enqueue_cloud_publish.assert_awaited_once()
    assert svc.enqueue_cloud_publish.await_args is not None
    publish_args, publish_kwargs = svc.enqueue_cloud_publish.await_args
    assert f"{Topic.DIGITAL.value}/13/mode/response" in publish_args[0]
    assert publish_args[1] == b"OK"
    assert publish_kwargs["reply_context"] == inbound


@pytest.mark.asyncio
async def test_publish_cloud_message_wraps_command_response(
    test_config: RuntimeConfig, mock_bridge_state: RuntimeState
) -> None:
    """Validate that _publish_cloud_message builds pb.CommandResponse envelope when correlation_data is present."""
    svc = BridgeService(test_config, mock_bridge_state, MagicMock())

    stream_mock = AsyncMock()
    setattr(svc, "_cloud_stream", stream_mock)

    seq_id = 999
    msg = pb.CloudQueuedPublish(
        topic_name=f"br/{Topic.DIGITAL.value}/13/response",
        payload=b"OK",
        correlation_data=seq_id.to_bytes(8, "big"),
        response_topic="cloud",
    )

    publish_cloud_msg: Callable[[pb.CloudQueuedPublish], Awaitable[bool]] = getattr(svc, "_publish_cloud_message")
    published = await publish_cloud_msg(msg)
    assert published is True

    stream_mock.send_message.assert_awaited_once()
    envelope: pb.CloudEnvelope = stream_mock.send_message.await_args[0][0]
    assert envelope.sequence_id == seq_id
    assert envelope.WhichOneof("payload") == "command_response"
    assert envelope.command_response.status_code == 200
    assert envelope.command_response.payload == b"OK"


@pytest.mark.asyncio
async def test_mcu_pin_update_event_forwarding(test_config: RuntimeConfig, mock_bridge_state: RuntimeState) -> None:
    """Validate that PinUpdateEvent received from MCU updates count and enqueues cloud publish."""
    svc = BridgeService(test_config, mock_bridge_state, MagicMock())
    svc.enqueue_cloud = AsyncMock()

    initial_events = mock_bridge_state.pin_events_count
    event = pb.PinUpdateEvent(pin=5, value=512, timestamp_micros=1234567)

    on_pin_update: Callable[[int, pb.PinUpdateEvent], Awaitable[None]] = getattr(svc, "_on_mcu_pin_update_event")
    await on_pin_update(0, event)

    assert mock_bridge_state.pin_events_count == initial_events + 1
    svc.enqueue_cloud.assert_awaited_once()
    assert svc.enqueue_cloud.await_args is not None
    queued_msg: pb.CloudQueuedPublish = svc.enqueue_cloud.await_args[0][0]
    assert f"{Topic.DIGITAL.value}/5/update" in queued_msg.topic_name
    assert queued_msg.payload == b"512"
