"""Unit tests validating cloud command request/response loop and telemetry parity (SIL-2)."""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from typing import Any, cast
from unittest.mock import AsyncMock

from mcubridge.protocol import cloud_gateway_pb2, mcu_wire_pb2
from hypothesis import given
from hypothesis import strategies as st
from mcubridge.config.settings import RuntimeConfig
from mcubridge.protocol import protocol
from mcubridge.services.runtime import BridgeService
from mcubridge.state.context import RuntimeState
from mcubridge.transport.serial import SerialTransport


def _make_config() -> RuntimeConfig:
    return RuntimeConfig(
        allowed_commands=("echo", "ls"),
        serial_shared_secret=b"testsharedsecret",
        allow_non_tmp_paths=True,
    )


def _make_service() -> tuple[BridgeService, RuntimeState, AsyncMock, AsyncMock]:
    cfg = _make_config()
    # Production builds the state from the config (create_runtime_state); cloud
    # authorization is deny-by-default, so carry the config's permissions over.
    state = RuntimeState(cloud_topic_prefix=cfg.topic_prefix, topic_authorization=cfg.topic_authorization)
    state.link_sync_event.set()
    serial_mock = AsyncMock(spec=SerialTransport)
    serial_mock.send.return_value = True
    mock_publisher = AsyncMock()
    svc = BridgeService(cfg, state, serial_mock, cloud_publisher=mock_publisher)
    return svc, state, serial_mock, mock_publisher


@given(
    pin=st.integers(0, 64),
    val=st.integers(0, 2),
    is_mode=st.booleans(),
)
async def test_cloud_command_pin_dispatch_property(
    pin: int,
    val: int,
    is_mode: bool,
) -> None:
    """Validate that cloud pin commands (digital write and mode) dispatch RPC and responses."""
    svc, state, serial_mock, _ = _make_service()
    try:
        method = "rpc.pb.GpioService/SetPinMode" if is_mode else "rpc.pb.GpioService/DigitalWrite"
        payload = (
            mcu_wire_pb2.PinMode(pin=pin, mode=cast(Any, val)).SerializeToString()
            if is_mode
            else mcu_wire_pb2.DigitalWrite(pin=pin, value=val).SerializeToString()
        )

        serial_mock.send.return_value = cloud_gateway_pb2.GenericResponse(status="ok")
        resp_bytes = await svc.execute_rpc(method, payload)

        # Validate serial command was dispatched
        serial_mock.send.assert_awaited_once()
        assert serial_mock.send.await_args is not None
        args, _ = serial_mock.send.await_args
        if is_mode:
            assert args[0] in ("rpc.pb.GpioService/SetPinMode", protocol.get_operation("rpc.pb.GpioService/SetPinMode"))
            assert isinstance(args[1], mcu_wire_pb2.PinMode)
            assert args[1].pin == pin
        else:
            assert args[0] in (
                "rpc.pb.GpioService/DigitalWrite",
                protocol.get_operation("rpc.pb.GpioService/DigitalWrite"),
            )
            assert isinstance(args[1], mcu_wire_pb2.DigitalWrite)
            assert args[1].pin == pin
            assert args[1].value == val

        assert cloud_gateway_pb2.GenericResponse.FromString(resp_bytes).status == "ok"
    finally:
        state.cleanup()
        svc.cleanup()


@given(
    payload=st.binary(min_size=1, max_size=64),
)
async def test_publish_cloud_message_wraps_command_response(
    payload: bytes,
) -> None:
    """Validate that publish_cloud_message routes payload through send_cloud_telemetry."""
    svc, state, _, _ = _make_service()
    try:
        mock_client = AsyncMock()
        mock_writer = AsyncMock()
        svc.cloud_rpc_client = mock_client
        svc.cloud_writer = mock_writer
        svc.send_cloud_telemetry = AsyncMock(return_value=True)

        msg = cloud_gateway_pb2.CloudQueuedPublish(
            topic_name="br/res/rpc.pb.GpioService/DigitalRead",
            payload=payload,
        )

        publish_cloud_msg: Callable[[cloud_gateway_pb2.CloudQueuedPublish], Awaitable[bool]] = svc.publish_cloud_message
        published = await publish_cloud_msg(msg)
        assert published is True

        svc.send_cloud_telemetry.assert_awaited_once_with(system_status_blob=payload)
    finally:
        state.cleanup()
        svc.cleanup()


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
    svc, state, _, mock_cloud = _make_service()
    try:
        initial_events = state.pin_events_count
        event = mcu_wire_pb2.PinUpdateEvent(pin=pin, value=value, timestamp_micros=timestamp)

        on_pin_update: Callable[[mcu_wire_pb2.PinUpdateEvent], Awaitable[None]] = svc.gpio.handle_pin_update_event
        await on_pin_update(event)

        assert state.pin_events_count == initial_events + 1
        mock_cloud.assert_awaited_once()
        assert mock_cloud.await_args is not None
        call_args = mock_cloud.await_args.args
        assert len(call_args) > 0
        queued_msg: cloud_gateway_pb2.CloudQueuedPublish = call_args[0]
        assert f"evt/rpc.pb.GpioService/PinUpdateEvent/{pin}" in queued_msg.topic_name
        assert queued_msg.payload == str(value).encode()
    finally:
        state.cleanup()
        svc.cleanup()
