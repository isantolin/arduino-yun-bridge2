"""Unit tests validating cloud command request/response loop and telemetry parity (SIL-2)."""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from typing import cast
from unittest.mock import AsyncMock

from hypothesis import given, settings, strategies as st
import pytest

from mcubridge.config.settings import RuntimeConfig
import mcubridge.protocol.mcubridge_pb2 as pb
from mcubridge.protocol.protocol import Command, Topic
from mcubridge.protocol.topics import TopicRoute, parse_topic, topic_path
from mcubridge.services.runtime import BridgeService
from mcubridge.state.context import RuntimeState
from mcubridge.transport.serial import SerialTransport


def _make_config() -> RuntimeConfig:
    return RuntimeConfig(
        allowed_commands=("echo", "ls"),
        serial_shared_secret=b"testsharedsecret",
        allow_non_tmp_paths=True,
    )


def _make_service() -> tuple[BridgeService, RuntimeState, AsyncMock, AsyncMock, AsyncMock]:
    cfg = _make_config()
    state = RuntimeState(cloud_topic_prefix=cfg.topic_prefix)
    serial_mock = AsyncMock(spec=SerialTransport)
    serial_mock.send.return_value = True
    svc = BridgeService(cfg, state, serial_mock)
    mock_publish = AsyncMock()
    mock_cloud = AsyncMock()
    setattr(svc, "enqueue_cloud_publish", mock_publish)
    setattr(svc, "enqueue_cloud", mock_cloud)
    return svc, state, serial_mock, mock_publish, mock_cloud


@pytest.mark.asyncio
@settings(max_examples=25, derandomize=True, deadline=None)
@given(
    pin=st.integers(0, 64),
    val=st.integers(0, 2),
    corr_id=st.integers(1, 2**63 - 1),
    is_mode=st.booleans(),
)
async def test_cloud_command_pin_dispatch_property(
    pin: int,
    val: int,
    corr_id: int,
    is_mode: bool,
) -> None:
    """Validate that cloud pin commands (digital write and mode) dispatch correlated RPC and responses."""
    svc, state, serial_mock, mock_publish, _ = _make_service()

    args_path = [str(pin), "mode"] if is_mode else [str(pin)]
    t_path = topic_path(state.cloud_topic_prefix, Topic.DIGITAL, *args_path)
    route = parse_topic(state.cloud_topic_prefix, t_path)
    assert route is not None

    inbound = pb.CloudQueuedPublish(
        topic_name=t_path,
        payload=str(val).encode(),
        correlation_data=corr_id.to_bytes(8, "big"),
        response_topic="cloud",
    )

    handle_pin: Callable[[TopicRoute, pb.CloudQueuedPublish], Awaitable[None]] = getattr(svc, "_handle_pin")
    await handle_pin(route, inbound)

    # Validate serial command was dispatched
    serial_mock.send.assert_awaited_once()
    assert serial_mock.send.await_args is not None
    args, _ = serial_mock.send.await_args
    mock_publish.assert_awaited_once()
    assert mock_publish.await_args is not None
    if is_mode:
        assert args[0] == Command.CMD_SET_PIN_MODE.value
        assert isinstance(args[1], pb.PinMode)
        assert args[1].pin == pin
        assert f"{Topic.DIGITAL.value}/{pin}/mode/response" in str(mock_publish.await_args.args[0])
    else:
        assert args[0] == Command.CMD_DIGITAL_WRITE.value
        assert isinstance(args[1], pb.DigitalWrite)
        assert args[1].pin == pin
        assert args[1].value == val
        assert f"{Topic.DIGITAL.value}/{pin}/response" in str(mock_publish.await_args.args[0])

    # Validate cloud response was enqueued with correlation context
    assert mock_publish.await_args.args[1] == b"OK"
    assert mock_publish.await_args.kwargs["reply_context"] == inbound


@pytest.mark.asyncio
@settings(max_examples=25, derandomize=True, deadline=None)
@given(
    seq_id=st.integers(1, 2**63 - 1),
    payload=st.binary(min_size=1, max_size=64),
)
async def test_publish_cloud_message_wraps_command_response(
    seq_id: int,
    payload: bytes,
) -> None:
    """Validate that _publish_cloud_message builds pb.CommandResponse envelope when correlation_data is present."""
    svc, _state, _, _, _ = _make_service()
    stream_mock = AsyncMock()
    setattr(svc, "_cloud_stream", stream_mock)

    msg = pb.CloudQueuedPublish(
        topic_name=f"br/{Topic.DIGITAL.value}/13/response",
        payload=payload,
        correlation_data=seq_id.to_bytes(8, "big"),
        response_topic="cloud",
    )

    publish_cloud_msg: Callable[[pb.CloudQueuedPublish], Awaitable[bool]] = getattr(svc, "_publish_cloud_message")
    published = await publish_cloud_msg(msg)
    assert published is True

    stream_mock.send_message.assert_awaited_once()
    assert stream_mock.send_message.await_args is not None
    envelope = cast(pb.CloudEnvelope, stream_mock.send_message.await_args.args[0])
    assert envelope.sequence_id == seq_id
    assert envelope.WhichOneof("payload") == "command_response"
    assert envelope.command_response.status_code == 200
    assert envelope.command_response.payload == payload


@pytest.mark.asyncio
@settings(max_examples=25, derandomize=True, deadline=None)
@given(
    pin=st.integers(0, 64),
    value=st.integers(0, 1023),
    timestamp=st.integers(0, 0xFFFFFFFF),
)
async def test_mcu_pin_update_event_forwarding(
    pin: int,
    value: int,
    timestamp: int,
) -> None:
    """Validate that PinUpdateEvent received from MCU updates count and enqueues cloud publish."""
    svc, state, _, _, mock_cloud = _make_service()

    initial_events = state.pin_events_count
    event = pb.PinUpdateEvent(pin=pin, value=value, timestamp_micros=timestamp)

    on_pin_update: Callable[[int, pb.PinUpdateEvent], Awaitable[None]] = getattr(svc, "_on_mcu_pin_update_event")
    await on_pin_update(0, event)

    assert state.pin_events_count == initial_events + 1
    mock_cloud.assert_awaited_once()
    assert mock_cloud.await_args is not None
    call_args = mock_cloud.await_args.args
    assert len(call_args) > 0
    queued_msg: pb.CloudQueuedPublish = call_args[0]
    assert f"{Topic.DIGITAL.value}/{pin}/update" in queued_msg.topic_name
    assert queued_msg.payload == str(value).encode()
