"""Pin/system component integration tests for BridgeService."""

from __future__ import annotations

import asyncio
import msgspec
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from aiomqtt.message import Message
from mcubridge.config.settings import RuntimeConfig
from mcubridge.protocol import protocol, structures
from mcubridge.protocol.protocol import Command, Status
from mcubridge.protocol.topics import Topic, topic_path
from mcubridge.services.runtime import BridgeService
from mcubridge.state.context import (
    PendingPinRequest,
    RuntimeState,
)


@pytest.mark.asyncio
async def test_mcu_digital_read_response_publishes_to_mqtt(
    runtime_config: RuntimeConfig,
    runtime_state: RuntimeState,
) -> None:
    enqueue_mqtt = AsyncMock()
    service = BridgeService(runtime_config, runtime_state, enqueue_mqtt)
    runtime_state.mark_synchronized()

    sent_frames: list[tuple[int, bytes]] = []

    async def fake_sender(
        command_id: int, payload: bytes, seq_id: int | None = None
    ) -> bool:
        sent_frames.append((command_id, payload))
        return True

    service.serial_flow.set_sender(fake_sender)

    runtime_state.pending_digital_reads.append(
        PendingPinRequest(pin=7, reply_context=None)
    )

    payload = msgspec.msgpack.encode(structures.DigitalReadResponsePacket(value=1))
    await service.handle_mcu_frame(Command.CMD_DIGITAL_READ_RESP.value, 0, payload)

    enqueue_mqtt.assert_called()
    msg = enqueue_mqtt.call_args.args[0]
    expected_topic = topic_path(
        runtime_state.mqtt_topic_prefix,
        Topic.DIGITAL,
        "value",
        "7",
    )
    assert msg.topic_name == expected_topic
    assert msg.payload == b"1"

    assert sent_frames
    ack_id, ack_payload = sent_frames[-1]
    assert ack_id == Status.ACK.value


@pytest.mark.asyncio
async def test_mcu_analog_read_response_publishes_to_mqtt(
    runtime_config: RuntimeConfig,
    runtime_state: RuntimeState,
) -> None:
    enqueue_mqtt = AsyncMock()
    service = BridgeService(runtime_config, runtime_state, enqueue_mqtt)
    runtime_state.mark_synchronized()

    sent_frames: list[tuple[int, bytes]] = []

    async def fake_sender(
        command_id: int, payload: bytes, seq_id: int | None = None
    ) -> bool:
        sent_frames.append((command_id, payload))
        return True

    service.serial_flow.set_sender(fake_sender)

    runtime_state.pending_analog_reads.append(
        PendingPinRequest(pin=3, reply_context=None)
    )

    TEST_EXIT_CODE = 0x7F
    payload = msgspec.msgpack.encode(
        structures.AnalogReadResponsePacket(value=TEST_EXIT_CODE)
    )
    await service.handle_mcu_frame(Command.CMD_ANALOG_READ_RESP.value, 0, payload)

    enqueue_mqtt.assert_called()
    msg = enqueue_mqtt.call_args.args[0]
    expected_topic = topic_path(
        runtime_state.mqtt_topic_prefix,
        Topic.ANALOG,
        "value",
        "3",
    )
    assert msg.topic_name == expected_topic
    assert msg.payload == b"127"

    assert sent_frames
    ack_id, ack_payload = sent_frames[-1]
    assert ack_id == Status.ACK.value


@pytest.mark.asyncio
async def test_mqtt_digital_write_sends_frame(
    runtime_config: RuntimeConfig,
    runtime_state: RuntimeState,
) -> None:
    service = BridgeService(runtime_config, runtime_state, AsyncMock())
    # Ensure synchronized and allowed for MQTT dispatch
    runtime_state.mark_synchronized()
    service.dispatcher.is_topic_action_allowed = MagicMock(return_value=True)

    sent_frames: list[tuple[int, bytes]] = []
    flow = service.serial_flow

    async def fake_sender(
        command_id: int, payload: bytes, seq_id: int | None = None
    ) -> bool:
        sent_frames.append((command_id, payload))
        # Immediate ACK to flow controller to satisfy its wait
        flow.on_frame_received(
            Status.ACK.value,
            seq_id or 0,
            b"",
        )
        return True

    flow.set_sender(fake_sender)

    await service.handle_mqtt_message(
        Message(
            topic=topic_path(
                runtime_state.mqtt_topic_prefix,
                Topic.DIGITAL,
                "5",
                "write",
            ),
            payload=b"1",
            qos=0,
            retain=False,
            mid=1,
            properties=None,
        )
    )

    assert sent_frames
    command_id, payload = sent_frames[0]
    assert command_id == Command.CMD_DIGITAL_WRITE.value


@pytest.mark.asyncio
async def test_mqtt_analog_read_tracks_pending_queue(
    runtime_config: RuntimeConfig,
    runtime_state: RuntimeState,
) -> None:
    service = BridgeService(runtime_config, runtime_state, AsyncMock())
    runtime_state.mark_synchronized()
    service.dispatcher.is_topic_action_allowed = MagicMock(return_value=True)

    sent_frames: list[tuple[int, bytes]] = []
    flow = service.serial_flow

    async def fake_sender(
        command_id: int, payload: bytes, seq_id: int | None = None
    ) -> bool:
        sent_frames.append((command_id, payload))
        return True

    flow.set_sender(fake_sender)

    await service.handle_mqtt_message(
        Message(
            topic=topic_path(
                runtime_state.mqtt_topic_prefix,
                Topic.ANALOG,
                "2",
                "get",
            ),
            payload=b"",
            qos=0,
            retain=False,
            mid=1,
            properties=None,
        )
    )

    assert sent_frames
    command_id, payload = sent_frames[0]
    assert command_id == Command.CMD_ANALOG_READ.value
    assert payload == msgspec.msgpack.encode(structures.PinReadPacket(pin=2))
    pending = runtime_state.pending_analog_reads[-1]
    assert pending.pin == 2


@pytest.mark.asyncio
async def test_mcu_unknown_request_yields_unknown_command_metrics(
    runtime_config: RuntimeConfig,
    runtime_state: RuntimeState,
) -> None:
    service = BridgeService(runtime_config, runtime_state, AsyncMock())
    # MUST be synchronized to reach the increment in dispatcher
    runtime_state.mark_synchronized()

    await service.handle_mcu_frame(
        0x99,  # Truly unknown command
        0,
        b"",
    )

    assert runtime_state.unknown_command_count == 1


@pytest.mark.asyncio
async def test_mcu_free_memory_response_enqueues_value(
    runtime_config: RuntimeConfig,
    runtime_state: RuntimeState,
) -> None:
    enqueue_mqtt = AsyncMock()
    service = BridgeService(runtime_config, runtime_state, enqueue_mqtt)
    runtime_state.mark_synchronized()

    sent_frames: list[tuple[int, bytes]] = []

    async def fake_sender(
        command_id: int, payload: bytes, seq_id: int | None = None
    ) -> bool:
        sent_frames.append((command_id, payload))
        return True

    service.serial_flow.set_sender(fake_sender)

    payload = msgspec.msgpack.encode(structures.FreeMemoryResponsePacket(value=100))
    await service.handle_mcu_frame(Command.CMD_GET_FREE_MEMORY_RESP.value, 0, payload)

    enqueue_mqtt.assert_called()
    msg = enqueue_mqtt.call_args.args[0]
    expected_topic = topic_path(
        runtime_state.mqtt_topic_prefix,
        Topic.SYSTEM,
        "free_memory",
        "value",
    )
    assert msg.topic_name == expected_topic
    assert msg.payload == b"100"

    assert sent_frames
    ack_id, ack_payload = sent_frames[-1]
    assert ack_id == Status.ACK.value


@pytest.mark.asyncio
async def test_mqtt_system_version_get_requests_and_publishes_cached(
    runtime_config: RuntimeConfig,
    runtime_state: RuntimeState,
) -> None:
    enqueue_mqtt = AsyncMock()
    service = BridgeService(runtime_config, runtime_state, enqueue_mqtt)
    runtime_state.mark_synchronized()

    runtime_state.mcu_version = (1, 2, 0)

    sent_frames: list[tuple[int, bytes]] = []
    flow = service.serial_flow

    async def fake_sender(
        command_id: int, payload: bytes, seq_id: int | None = None
    ) -> bool:
        sent_frames.append((command_id, payload))
        return True

    flow.set_sender(fake_sender)

    await service.handle_mqtt_message(
        Message(
            topic=topic_path(
                runtime_state.mqtt_topic_prefix,
                Topic.SYSTEM,
                "version",
                "get",
            ),
            payload=b"",
            qos=0,
            retain=False,
            mid=1,
            properties=None,
        )
    )

    # Wait for background task to complete (it sets version to None)
    for _ in range(200):
        if runtime_state.mcu_version is None:
            break
        await asyncio.sleep(0.01)

    assert sent_frames
    assert sent_frames[0][0] == Command.CMD_GET_VERSION.value
    assert runtime_state.mcu_version is None

    # Simulate response
    payload = msgspec.msgpack.encode(structures.VersionResponsePacket(major=1, minor=2, patch=0))
    await service.handle_mcu_frame(Command.CMD_GET_VERSION_RESP.value, 0, payload)

    enqueue_mqtt.assert_called()
    msg = enqueue_mqtt.call_args.args[0]
    expected_topic = topic_path(
        runtime_state.mqtt_topic_prefix,
        Topic.SYSTEM,
        "version",
        "value",
    )
    assert msg.topic_name == expected_topic
    assert msg.payload == b"1.2.0"


@pytest.mark.asyncio
async def test_mqtt_shell_kill_invokes_processonent(
    runtime_config: RuntimeConfig,
    runtime_state: RuntimeState,
) -> None:
    service = BridgeService(runtime_config, runtime_state, AsyncMock())
    process = service.process
    runtime_state.mark_synchronized()

    with patch.object(process, "handle_mqtt", new_callable=AsyncMock) as mock_mqtt:
        # Re-register mock in dispatcher manually
        service.dispatcher.mqtt_handlers[Topic.SHELL] = [mock_mqtt]
        
        pid = 21
        await service.handle_mqtt_message(
            Message(
                topic=topic_path(
                    runtime_state.mqtt_topic_prefix,
                    Topic.SHELL,
                    "kill",
                    str(pid),
                ),
                payload=b"",
                qos=0,
                retain=False,
                mid=1,
                properties=None,
            )
        )
        # ProcessComponent handles shell topics
        mock_mqtt.assert_called()
        route = mock_mqtt.call_args[0][0]
        assert route.segments == ("kill", str(pid))
