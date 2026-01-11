"""Unit tests for mcubridge.services.components.pin."""

from __future__ import annotations

import struct
from collections.abc import Coroutine
from typing import Any, cast
from unittest.mock import AsyncMock

import pytest

from aiomqtt.message import Message as MQTTMessage

from mcubridge.config.settings import RuntimeConfig
from mcubridge.mqtt.messages import QueuedPublish
from mcubridge.protocol.topics import Topic, topic_path
from mcubridge.rpc import protocol
from mcubridge.rpc.protocol import Command, PinAction, Status
from mcubridge.services.components.pin import PinComponent
from mcubridge.state.context import PendingPinRequest, RuntimeState


class RecordingBridgeContext:
    def __init__(self, config: RuntimeConfig, state: RuntimeState) -> None:
        self.config = config
        self.state = state
        self.sent_frames: list[tuple[int, bytes]] = []
        self.enqueued: list[tuple[QueuedPublish, MQTTMessage | None]] = []
        self.send_frame_result = True

    async def send_frame(self, command_id: int, payload: bytes = b"") -> bool:
        self.sent_frames.append((command_id, payload))
        return self.send_frame_result

    async def enqueue_mqtt(
        self, message: QueuedPublish, *, reply_context: MQTTMessage | None = None
    ) -> None:
        self.enqueued.append((message, reply_context))

    def is_command_allowed(self, command: str) -> bool:
        return True

    async def schedule_background(
        self,
        coroutine: Coroutine[Any, Any, None],
        *,
        name: str | None = None,
    ) -> Any:
        task = AsyncMock()
        await coroutine
        return task


def _fake_inbound() -> MQTTMessage:
    return cast(MQTTMessage, object())


@pytest.mark.asyncio
async def test_handle_unexpected_mcu_request_sends_not_implemented(
    runtime_config: RuntimeConfig,
    runtime_state: RuntimeState,
) -> None:
    ctx = RecordingBridgeContext(runtime_config, runtime_state)
    component = PinComponent(runtime_config, runtime_state, ctx)

    ok = await component.handle_unexpected_mcu_request(
        Command.CMD_DIGITAL_READ,
        b"\x01\x02",
    )

    assert ok is False
    assert ctx.sent_frames
    command_id, payload = ctx.sent_frames[-1]
    assert command_id == Status.NOT_IMPLEMENTED.value
    assert b"pin-read-origin-mcu" in payload


@pytest.mark.asyncio
async def test_handle_unexpected_mcu_request_unknown_command(
    runtime_config: RuntimeConfig,
    runtime_state: RuntimeState,
) -> None:
    ctx = RecordingBridgeContext(runtime_config, runtime_state)
    component = PinComponent(runtime_config, runtime_state, ctx)

    ok = await component.handle_unexpected_mcu_request(
        Command.CMD_GET_VERSION,
        b"",
    )

    assert ok is False
    command_id, payload = ctx.sent_frames[-1]
    assert command_id == Status.NOT_IMPLEMENTED.value
    assert b"pin_request_not_supported" in payload


@pytest.mark.asyncio
async def test_handle_digital_read_resp_malformed_payload_is_ignored(
    runtime_config: RuntimeConfig,
    runtime_state: RuntimeState,
) -> None:
    ctx = RecordingBridgeContext(runtime_config, runtime_state)
    component = PinComponent(runtime_config, runtime_state, ctx)

    await component.handle_digital_read_resp(b"")

    assert ctx.enqueued == []


@pytest.mark.asyncio
async def test_handle_digital_read_resp_without_pending_request_publishes_unknown_pin(
    runtime_config: RuntimeConfig,
    runtime_state: RuntimeState,
) -> None:
    ctx = RecordingBridgeContext(runtime_config, runtime_state)
    component = PinComponent(runtime_config, runtime_state, ctx)

    await component.handle_digital_read_resp(struct.pack(protocol.PIN_READ_FORMAT, 1))

    assert len(ctx.enqueued) == 1
    message, reply_context = ctx.enqueued[0]
    assert reply_context is None
    assert message.payload == b"1"
    assert message.topic_name == topic_path(
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

    ctx = RecordingBridgeContext(runtime_config, runtime_state)
    component = PinComponent(runtime_config, runtime_state, ctx)

    await component.handle_digital_read_resp(bytes([protocol.DIGITAL_LOW]))

    message, reply_context = ctx.enqueued[0]
    assert reply_context is inbound
    assert message.topic_name == topic_path(
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

    ctx = RecordingBridgeContext(runtime_config, runtime_state)
    component = PinComponent(runtime_config, runtime_state, ctx)

    await component.handle_analog_read_resp(struct.pack(protocol.UINT16_FORMAT, 256))

    message, reply_context = ctx.enqueued[0]
    assert reply_context is inbound
    assert message.payload == b"256"
    assert message.topic_name == topic_path(
        runtime_state.mqtt_topic_prefix,
        Topic.ANALOG,
        "3",
        "value",
    )


@pytest.mark.asyncio
async def test_handle_mqtt_mode_command_valid_payload_sends_frame(
    runtime_config: RuntimeConfig,
    runtime_state: RuntimeState,
) -> None:
    ctx = RecordingBridgeContext(runtime_config, runtime_state)
    component = PinComponent(runtime_config, runtime_state, ctx)

    await component.handle_mqtt(
        Topic.DIGITAL,
        [
            runtime_state.mqtt_topic_prefix,
            Topic.DIGITAL.value,
            "2",
            PinAction.MODE.value,
        ],
        "1",
    )

    assert ctx.sent_frames
    command_id, payload = ctx.sent_frames[-1]
    assert command_id == Command.CMD_SET_PIN_MODE.value
    assert payload == struct.pack(protocol.PIN_WRITE_FORMAT, 2, 1)


@pytest.mark.asyncio
async def test_handle_mqtt_mode_command_rejects_invalid_payload(
    runtime_config: RuntimeConfig,
    runtime_state: RuntimeState,
) -> None:
    ctx = RecordingBridgeContext(runtime_config, runtime_state)
    component = PinComponent(runtime_config, runtime_state, ctx)

    await component.handle_mqtt(
        Topic.DIGITAL,
        [
            runtime_state.mqtt_topic_prefix,
            Topic.DIGITAL.value,
            "2",
            PinAction.MODE.value,
        ],
        "not-an-int",
    )

    assert ctx.sent_frames == []


@pytest.mark.asyncio
async def test_handle_mqtt_read_command_queue_overflow_notifies_mqtt(
    runtime_config: RuntimeConfig,
    runtime_state: RuntimeState,
) -> None:
    runtime_state.pending_pin_request_limit = 1
    runtime_state.pending_digital_reads.append(PendingPinRequest(pin=1, reply_context=None))

    inbound = _fake_inbound()
    ctx = RecordingBridgeContext(runtime_config, runtime_state)
    component = PinComponent(runtime_config, runtime_state, ctx)

    await component.handle_mqtt(
        Topic.DIGITAL,
        [
            runtime_state.mqtt_topic_prefix,
            Topic.DIGITAL.value,
            "9",
            PinAction.READ.value,
        ],
        "",
        inbound,
    )

    assert ctx.sent_frames == []
    assert len(ctx.enqueued) == 1
    message, reply_context = ctx.enqueued[0]
    assert reply_context is inbound
    assert message.topic_name == topic_path(
        runtime_state.mqtt_topic_prefix,
        Topic.DIGITAL,
        "9",
        "value",
    )
    assert message.payload == b""
    assert ("bridge-error", "pending-pin-overflow") in message.user_properties


@pytest.mark.asyncio
async def test_handle_mqtt_read_command_send_fails_does_not_enqueue_pending(
    runtime_config: RuntimeConfig,
    runtime_state: RuntimeState,
) -> None:
    ctx = RecordingBridgeContext(runtime_config, runtime_state)
    ctx.send_frame_result = False
    component = PinComponent(runtime_config, runtime_state, ctx)

    await component.handle_mqtt(
        Topic.ANALOG,
        [
            runtime_state.mqtt_topic_prefix,
            Topic.ANALOG.value,
            "3",
            PinAction.READ.value,
        ],
        "",
    )

    assert not runtime_state.pending_analog_reads


@pytest.mark.asyncio
async def test_handle_mqtt_read_command_appends_pending_on_success(
    runtime_config: RuntimeConfig,
    runtime_state: RuntimeState,
) -> None:
    inbound = _fake_inbound()
    ctx = RecordingBridgeContext(runtime_config, runtime_state)
    component = PinComponent(runtime_config, runtime_state, ctx)

    await component.handle_mqtt(
        Topic.ANALOG,
        [
            runtime_state.mqtt_topic_prefix,
            Topic.ANALOG.value,
            "3",
            PinAction.READ.value,
        ],
        "",
        inbound,
    )

    assert ctx.sent_frames
    command_id, payload = ctx.sent_frames[-1]
    assert command_id == Command.CMD_ANALOG_READ.value
    assert payload == struct.pack(protocol.PIN_READ_FORMAT, 3)
    assert runtime_state.pending_analog_reads
    request = runtime_state.pending_analog_reads[-1]
    assert request.pin == 3
    assert request.reply_context is inbound


@pytest.mark.asyncio
async def test_handle_mqtt_write_digital_accepts_empty_payload_as_zero(
    runtime_config: RuntimeConfig,
    runtime_state: RuntimeState,
) -> None:
    ctx = RecordingBridgeContext(runtime_config, runtime_state)
    component = PinComponent(runtime_config, runtime_state, ctx)

    await component.handle_mqtt(
        Topic.DIGITAL,
        [runtime_state.mqtt_topic_prefix, Topic.DIGITAL.value, "5"],
        "",
    )

    command_id, payload = ctx.sent_frames[-1]
    assert command_id == Command.CMD_DIGITAL_WRITE.value
    assert payload == struct.pack(protocol.PIN_WRITE_FORMAT, 5, protocol.DIGITAL_LOW)


@pytest.mark.asyncio
async def test_handle_mqtt_write_rejects_invalid_payload(
    runtime_config: RuntimeConfig,
    runtime_state: RuntimeState,
) -> None:
    ctx = RecordingBridgeContext(runtime_config, runtime_state)
    component = PinComponent(runtime_config, runtime_state, ctx)

    await component.handle_mqtt(
        Topic.DIGITAL,
        [runtime_state.mqtt_topic_prefix, Topic.DIGITAL.value, "5"],
        "999",
    )

    assert ctx.sent_frames == []


@pytest.mark.asyncio
async def test_handle_mqtt_parses_analog_pin_identifier_prefix_a(
    runtime_config: RuntimeConfig,
    runtime_state: RuntimeState,
) -> None:
    ctx = RecordingBridgeContext(runtime_config, runtime_state)
    component = PinComponent(runtime_config, runtime_state, ctx)

    await component.handle_mqtt(
        Topic.ANALOG,
        [runtime_state.mqtt_topic_prefix, Topic.ANALOG.value, "A1"],
        "10",
    )

    command_id, payload = ctx.sent_frames[-1]
    assert command_id == Command.CMD_ANALOG_WRITE.value
    assert payload == struct.pack(protocol.PIN_WRITE_FORMAT, 1, 10)
