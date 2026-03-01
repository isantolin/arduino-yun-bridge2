"""Unit tests for mcubridge.services.pin."""

from __future__ import annotations

from collections.abc import Coroutine
from typing import Any, cast
from unittest.mock import AsyncMock

import pytest
from aiomqtt.message import Message
from mcubridge.config.settings import RuntimeConfig
from mcubridge.mqtt.messages import QueuedPublish
from mcubridge.protocol import protocol, structures
from mcubridge.protocol.protocol import Command, PinAction, Status
from mcubridge.protocol.topics import Topic, topic_path
from mcubridge.services.pin import PinComponent
from mcubridge.state.context import PendingPinRequest, RuntimeState


class RecordingBridgeContext:
    def __init__(self, config: RuntimeConfig, state: RuntimeState) -> None:
        self.config = config
        self.state = state
        self.sent_frames: list[tuple[int, bytes]] = []
        self.enqueued: list[tuple[QueuedPublish, Message | None]] = []
        self.send_frame_result = True

    async def send_frame(self, command_id: int, payload: bytes = b"") -> bool:
        self.sent_frames.append((command_id, payload))
        return self.send_frame_result

    async def publish(
        self,
        topic: str,
        payload: bytes | str,
        *,
        qos: int = 0,
        retain: bool = False,
        expiry: int | None = None,
        properties: tuple[tuple[str, str], ...] = (),
        content_type: str | None = None,
        reply_to: Message | None = None,
    ) -> None:
        if isinstance(payload, str):
            payload_bytes = payload.encode("utf-8")
        else:
            payload_bytes = payload

        message = QueuedPublish(
            topic_name=topic,
            payload=payload_bytes,
            qos=qos,
            retain=retain,
            content_type=content_type,
            message_expiry_interval=expiry,
            user_properties=properties,
        )
        self.enqueued.append((message, reply_to))

    async def enqueue_mqtt(self, message: QueuedPublish, *, reply_context: Message | None = None) -> None:
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


def _fake_inbound() -> Message:
    return cast(Message, object())


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

    await component.handle_digital_read_resp(structures.PinReadPacket._SCHEMA.build({"pin": 1}))

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
    runtime_state.pending_digital_reads.append(PendingPinRequest(pin=7, reply_context=inbound))

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
    runtime_state.pending_analog_reads.append(PendingPinRequest(pin=3, reply_context=inbound))

    ctx = RecordingBridgeContext(runtime_config, runtime_state)
    component = PinComponent(runtime_config, runtime_state, ctx)

    await component.handle_analog_read_resp(structures.UINT16_STRUCT.build(256))

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
    assert payload == structures.DigitalWritePacket._SCHEMA.build(dict(pin=2, value=1))


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
    assert payload == structures.PinReadPacket._SCHEMA.build({"pin": 3})
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
    assert payload == structures.DigitalWritePacket._SCHEMA.build(dict(pin=5, value=protocol.DIGITAL_LOW))


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
    assert payload == structures.DigitalWritePacket._SCHEMA.build(dict(pin=1, value=10))
