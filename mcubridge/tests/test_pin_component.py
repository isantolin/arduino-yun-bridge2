"""Unit tests for mcubridge.services.pin (SIL-2)."""

from __future__ import annotations

from typing import Any, cast
from unittest.mock import AsyncMock

import pytest
from aiomqtt.message import Message

from mcubridge.config.settings import RuntimeConfig
from mcubridge.protocol import protocol, structures
from mcubridge.protocol.protocol import Command, PinAction, Status
from mcubridge.protocol.topics import Topic, topic_path
from mcubridge.services.base import BridgeContext
from mcubridge.services.pin import PinComponent
from mcubridge.state.context import PendingPinRequest, RuntimeState
from tests._helpers import make_mqtt_msg, make_route


def _fake_inbound() -> Message:
    return cast(Message, object())


def _extract_enqueued_publish(ctx: AsyncMock, index: int = -1) -> tuple[structures.QueuedPublish, Any]:
    """Helper to extract QueuedPublish and reply_context from AsyncMock.publish calls."""
    call = ctx.publish.call_args_list[index]
    topic = call.kwargs.get("topic", call.args[0] if call.args else "")
    payload = call.kwargs.get("payload", call.args[1] if len(call.args) > 1 else b"")

    if isinstance(payload, str):
        payload = payload.encode("utf-8")

    msg = structures.QueuedPublish(
        topic_name=topic,
        payload=payload,
        qos=call.kwargs.get("qos", 0),
        retain=call.kwargs.get("retain", False),
        content_type=call.kwargs.get("content_type"),
        message_expiry_interval=call.kwargs.get("expiry"),
        user_properties=call.kwargs.get("properties", ()),
    )
    return msg, call.kwargs.get("reply_to")


@pytest.mark.asyncio
async def test_handle_mqtt_mode_command_valid_payload_sends_frame(
    runtime_config: RuntimeConfig, runtime_state: RuntimeState
) -> None:
    ctx = AsyncMock(spec=BridgeContext)
    ctx.config = runtime_config
    ctx.state = runtime_state
    ctx.send_frame.return_value = True

    component = PinComponent(runtime_config, runtime_state, ctx)

    # Pin 13 mode output (1)
    await component.handle_mqtt(
        make_route(Topic.DIGITAL, "13", PinAction.MODE.value),
        make_mqtt_msg("1"),
    )

    ctx.send_frame.assert_called_once_with(
        Command.CMD_SET_PIN_MODE.value,
        structures.PinModePacket(pin=13, mode=1).encode(),
    )


@pytest.mark.asyncio
async def test_handle_mqtt_digital_write_sends_frame(
    runtime_config: RuntimeConfig, runtime_state: RuntimeState
) -> None:
    ctx = AsyncMock(spec=BridgeContext)
    ctx.config = runtime_config
    ctx.state = runtime_state
    ctx.send_frame.return_value = True

    component = PinComponent(runtime_config, runtime_state, ctx)

    await component.handle_mqtt(
        make_route(Topic.DIGITAL, "2"),
        make_mqtt_msg("1"),
    )

    ctx.send_frame.assert_called_once_with(
        Command.CMD_DIGITAL_WRITE.value,
        structures.DigitalWritePacket(pin=2, value=protocol.DIGITAL_HIGH).encode(),
    )


@pytest.mark.asyncio
async def test_handle_mqtt_analog_write_sends_frame(
    runtime_config: RuntimeConfig, runtime_state: RuntimeState
) -> None:
    ctx = AsyncMock(spec=BridgeContext)
    ctx.config = runtime_config
    ctx.state = runtime_state
    ctx.send_frame.return_value = True

    component = PinComponent(runtime_config, runtime_state, ctx)

    await component.handle_mqtt(
        make_route(Topic.ANALOG, "5"),
        make_mqtt_msg("128"),
    )

    ctx.send_frame.assert_called_once_with(
        Command.CMD_ANALOG_WRITE.value,
        structures.AnalogWritePacket(pin=5, value=128).encode(),
    )


@pytest.mark.asyncio
async def test_handle_unexpected_mcu_request_sends_not_implemented(
    runtime_config: RuntimeConfig,
    runtime_state: RuntimeState,
) -> None:
    ctx = AsyncMock(spec=BridgeContext)
    ctx.config = runtime_config
    ctx.state = runtime_state
    ctx.send_frame.return_value = True

    component = PinComponent(runtime_config, runtime_state, ctx)

    ok = await component.handle_unexpected_mcu_request(
        0,
        Command.CMD_DIGITAL_READ,
        b"\x01\x02",
    )

    assert ok is False
    ctx.send_frame.assert_called_once()
    # Check that it sent Status.NOT_IMPLEMENTED (0x37)
    args = ctx.send_frame.call_args.args
    assert args[0] == Status.NOT_IMPLEMENTED.value


@pytest.mark.asyncio
async def test_handle_digital_read_resp_malformed_payload_is_ignored(
    runtime_config: RuntimeConfig,
    runtime_state: RuntimeState,
) -> None:
    ctx = AsyncMock(spec=BridgeContext)
    ctx.config = runtime_config
    ctx.state = runtime_state
    ctx.send_frame.return_value = True

    component = PinComponent(runtime_config, runtime_state, ctx)

    # Truncated varint — invalid protobuf
    await component.handle_digital_read_resp(0, b"\x80")

    ctx.publish.assert_not_called()


@pytest.mark.asyncio
async def test_handle_digital_read_resp_without_pending_request_publishes_unknown_pin(
    runtime_config: RuntimeConfig,
    runtime_state: RuntimeState,
) -> None:
    ctx = AsyncMock(spec=BridgeContext)
    ctx.config = runtime_config
    ctx.state = runtime_state
    ctx.send_frame.return_value = True

    component = PinComponent(runtime_config, runtime_state, ctx)

    await component.handle_digital_read_resp(
        0, structures.DigitalReadResponsePacket(value=protocol.DIGITAL_LOW).encode()
    )

    assert ctx.publish.call_count == 1
    msg, reply_to = _extract_enqueued_publish(ctx)
    assert reply_to is None
    assert msg.payload == b"0"
    assert msg.topic_name == topic_path(
        runtime_state.mqtt_topic_prefix,
        Topic.DIGITAL,
        "value",
    )


@pytest.mark.asyncio
async def test_handle_digital_read_resp_with_pending_request_uses_reply_context(
    runtime_config: RuntimeConfig,
    runtime_state: RuntimeState,
) -> None:
    inbound = _fake_inbound()
    runtime_state.pending_digital_reads.append(
        PendingPinRequest(pin=7, reply_context=inbound)
    )

    ctx = AsyncMock(spec=BridgeContext)
    ctx.config = runtime_config
    ctx.state = runtime_state
    ctx.send_frame.return_value = True

    component = PinComponent(runtime_config, runtime_state, ctx)

    await component.handle_digital_read_resp(
        0, structures.DigitalReadResponsePacket(value=protocol.DIGITAL_LOW).encode()
    )

    msg, reply_to = _extract_enqueued_publish(ctx)
    assert reply_to is inbound
    assert msg.topic_name == topic_path(
        runtime_state.mqtt_topic_prefix,
        Topic.DIGITAL,
        "7",
        "value",
    )


@pytest.mark.asyncio
async def test_handle_analog_read_resp_with_pending_request_decodes_big_endian(
    runtime_config: RuntimeConfig,
    runtime_state: RuntimeState,
) -> None:
    inbound = _fake_inbound()
    runtime_state.pending_analog_reads.append(
        PendingPinRequest(pin=3, reply_context=inbound)
    )

    ctx = AsyncMock(spec=BridgeContext)
    ctx.config = runtime_config
    ctx.state = runtime_state
    ctx.send_frame.return_value = True

    component = PinComponent(runtime_config, runtime_state, ctx)

    await component.handle_analog_read_resp(
        0, structures.AnalogReadResponsePacket(value=256).encode()
    )

    msg, reply_to = _extract_enqueued_publish(ctx)
    assert reply_to is inbound
    assert msg.payload == b"256"
    assert msg.topic_name == topic_path(
        runtime_state.mqtt_topic_prefix,
        Topic.ANALOG,
        "3",
        "value",
    )


@pytest.mark.asyncio
async def test_handle_mqtt_mode_command_rejects_invalid_payload(
    runtime_config: RuntimeConfig,
    runtime_state: RuntimeState,
) -> None:
    ctx = AsyncMock(spec=BridgeContext)
    ctx.config = runtime_config
    ctx.state = runtime_state
    ctx.send_frame.return_value = True

    component = PinComponent(runtime_config, runtime_state, ctx)

    await component.handle_mqtt(
        make_route(Topic.DIGITAL, "2", PinAction.MODE.value),
        make_mqtt_msg("not-an-int"),
    )

    ctx.send_frame.assert_not_called()


@pytest.mark.asyncio
async def test_handle_mqtt_read_command_queue_overflow_notifies_mqtt(
    runtime_config: RuntimeConfig,
    runtime_state: RuntimeState,
) -> None:
    runtime_state.pending_pin_request_limit = 1
    runtime_state.pending_digital_reads.append(
        PendingPinRequest(pin=1, reply_context=None)
    )

    inbound = make_mqtt_msg("")
    ctx = AsyncMock(spec=BridgeContext)
    ctx.config = runtime_config
    ctx.state = runtime_state
    ctx.send_frame.return_value = True

    component = PinComponent(runtime_config, runtime_state, ctx)

    await component.handle_mqtt(
        make_route(Topic.DIGITAL, "9", PinAction.READ.value),
        inbound,
    )

    ctx.send_frame.assert_not_called()
    assert ctx.publish.call_count == 1
    msg, reply_to = _extract_enqueued_publish(ctx)
    assert reply_to is inbound
    assert msg.topic_name == topic_path(
        runtime_state.mqtt_topic_prefix,
        Topic.DIGITAL,
        "9",
        "value",
    )
    assert msg.payload == b""
    assert ("bridge-error", "pending-pin-overflow") in msg.user_properties


@pytest.mark.asyncio
async def test_handle_mqtt_read_command_send_fails_does_not_enqueue_pending(
    runtime_config: RuntimeConfig,
    runtime_state: RuntimeState,
) -> None:
    ctx = AsyncMock(spec=BridgeContext)
    ctx.config = runtime_config
    ctx.state = runtime_state
    ctx.send_frame.return_value = False
    component = PinComponent(runtime_config, runtime_state, ctx)

    await component.handle_mqtt(
        make_route(Topic.ANALOG, "3", PinAction.READ.value),
        make_mqtt_msg(""),
    )

    assert not runtime_state.pending_analog_reads


@pytest.mark.asyncio
async def test_handle_mqtt_read_command_appends_pending_on_success(
    runtime_config: RuntimeConfig,
    runtime_state: RuntimeState,
) -> None:
    inbound = make_mqtt_msg("")
    ctx = AsyncMock(spec=BridgeContext)
    ctx.config = runtime_config
    ctx.state = runtime_state
    ctx.send_frame.return_value = True

    component = PinComponent(runtime_config, runtime_state, ctx)

    await component.handle_mqtt(
        make_route(Topic.ANALOG, "3", PinAction.READ.value),
        inbound,
    )

    ctx.send_frame.assert_called_once_with(
        Command.CMD_ANALOG_READ.value,
        structures.PinReadPacket(pin=3).encode(),
    )
    assert len(runtime_state.pending_analog_reads) == 1
    request = runtime_state.pending_analog_reads[-1]
    assert request.pin == 3
    assert request.reply_context is inbound


@pytest.mark.asyncio
async def test_handle_mqtt_write_digital_accepts_empty_payload_as_zero(
    runtime_config: RuntimeConfig,
    runtime_state: RuntimeState,
) -> None:
    ctx = AsyncMock(spec=BridgeContext)
    ctx.config = runtime_config
    ctx.state = runtime_state
    ctx.send_frame.return_value = True

    component = PinComponent(runtime_config, runtime_state, ctx)

    await component.handle_mqtt(
        make_route(Topic.DIGITAL, "5"),
        make_mqtt_msg(""),
    )

    ctx.send_frame.assert_called_once_with(
        Command.CMD_DIGITAL_WRITE.value,
        structures.DigitalWritePacket(
            pin=5, value=protocol.DIGITAL_LOW
        ).encode(),
    )


@pytest.mark.asyncio
async def test_handle_mqtt_write_rejects_invalid_payload(
    runtime_config: RuntimeConfig,
    runtime_state: RuntimeState,
) -> None:
    ctx = AsyncMock(spec=BridgeContext)
    ctx.config = runtime_config
    ctx.state = runtime_state
    ctx.send_frame.return_value = True

    component = PinComponent(runtime_config, runtime_state, ctx)

    await component.handle_mqtt(
        make_route(Topic.DIGITAL, "5"),
        make_mqtt_msg("999"),
    )

    ctx.send_frame.assert_not_called()


@pytest.mark.asyncio
async def test_handle_mqtt_parses_analog_pin_identifier_prefix_a(
    runtime_config: RuntimeConfig,
    runtime_state: RuntimeState,
) -> None:
    ctx = AsyncMock(spec=BridgeContext)
    ctx.config = runtime_config
    ctx.state = runtime_state
    ctx.send_frame.return_value = True

    component = PinComponent(runtime_config, runtime_state, ctx)

    await component.handle_mqtt(
        make_route(Topic.ANALOG, "A1"),
        make_mqtt_msg("10"),
    )

    ctx.send_frame.assert_called_once_with(
        Command.CMD_ANALOG_WRITE.value,
        structures.AnalogWritePacket(pin=1, value=10).encode(),
    )
