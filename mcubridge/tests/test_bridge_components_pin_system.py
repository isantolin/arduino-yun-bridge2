"""Pin/system component integration tests for BridgeService."""

from __future__ import annotations
import msgspec
from mcubridge.transport.mqtt import MqttTransport

from unittest.mock import AsyncMock, patch

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
    transport = MqttTransport(runtime_config, runtime_state)
    transport.enqueue_mqtt = AsyncMock()
    service = BridgeService(runtime_config, runtime_state, transport)

    sent_frames: list[tuple[int, bytes]] = []

    async def fake_sender(
        command_id: int, payload: bytes, seq_id: int | None = None
    ) -> bool:
        sent_frames.append((command_id, payload))
        return True

    service.register_serial_sender(fake_sender)

    runtime_state.pending_digital_reads.append(
        PendingPinRequest(pin=7, reply_context=None)
    )

    payload = msgspec.msgpack.encode(structures.DigitalReadResponsePacket(value=1))
    await service.handle_mcu_frame(Command.CMD_DIGITAL_READ_RESP.value, 0, payload)

    assert transport.enqueue_mqtt.call_count >= 1
    queued = transport.enqueue_mqtt.call_args_list[-1][0][0]
    expected_topic = topic_path(
        runtime_state.mqtt_topic_prefix,
        Topic.DIGITAL,
        "7",
        "value",
    )
    assert queued.topic_name == expected_topic
    assert queued.payload == b"1"
    # task done removed

    assert sent_frames
    ack_id, ack_payload = sent_frames[-1]
    assert ack_id == Status.ACK.value
    assert ack_payload == msgspec.msgpack.encode(
        structures.AckPacket(command_id=Command.CMD_DIGITAL_READ_RESP.value)
    )


@pytest.mark.asyncio
async def test_mcu_analog_read_response_publishes_to_mqtt(
    runtime_config: RuntimeConfig,
    runtime_state: RuntimeState,
) -> None:
    transport = MqttTransport(runtime_config, runtime_state)
    transport.enqueue_mqtt = AsyncMock()
    service = BridgeService(runtime_config, runtime_state, transport)

    sent_frames: list[tuple[int, bytes]] = []

    async def fake_sender(
        command_id: int, payload: bytes, seq_id: int | None = None
    ) -> bool:
        sent_frames.append((command_id, payload))
        return True

    service.register_serial_sender(fake_sender)

    runtime_state.pending_analog_reads.append(
        PendingPinRequest(pin=3, reply_context=None)
    )

    TEST_EXIT_CODE = 0x7F
    payload = msgspec.msgpack.encode(
        structures.AnalogReadResponsePacket(value=TEST_EXIT_CODE)
    )
    await service.handle_mcu_frame(Command.CMD_ANALOG_READ_RESP.value, 0, payload)

    assert transport.enqueue_mqtt.call_count >= 1
    queued = transport.enqueue_mqtt.call_args_list[-1][0][0]
    expected_topic = topic_path(
        runtime_state.mqtt_topic_prefix,
        Topic.ANALOG,
        "3",
        "value",
    )
    assert queued.topic_name == expected_topic
    assert queued.payload == b"127"
    # task done removed

    assert sent_frames
    ack_id, ack_payload = sent_frames[-1]
    assert ack_id == Status.ACK.value
    assert ack_payload == msgspec.msgpack.encode(
        structures.AckPacket(command_id=Command.CMD_ANALOG_READ_RESP.value)
    )


@pytest.mark.asyncio
async def test_mqtt_digital_write_sends_frame(
    runtime_config: RuntimeConfig,
    runtime_state: RuntimeState,
) -> None:
    transport = MqttTransport(runtime_config, runtime_state)
    transport.enqueue_mqtt = AsyncMock()
    service = BridgeService(runtime_config, runtime_state, transport)

    sent_frames: list[tuple[int, bytes]] = []
    flow = service.serial_flow

    async def fake_sender(
        command_id: int, payload: bytes, seq_id: int | None = None
    ) -> bool:
        sent_frames.append((command_id, payload))
        flow.on_frame_received(
            Status.ACK.value,
            seq_id or 0,
            msgspec.msgpack.encode(structures.AckPacket(command_id=command_id)),
        )
        return True

    service.register_serial_sender(fake_sender)

    await service.handle_mqtt_message(
        Message(
            topic=topic_path(
                runtime_state.mqtt_topic_prefix,
                Topic.DIGITAL,
                "5",
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
    assert payload == msgspec.msgpack.encode(
        structures.DigitalWritePacket(pin=5, value=protocol.DIGITAL_HIGH)
    )


@pytest.mark.asyncio
async def test_mqtt_analog_read_tracks_pending_queue(
    runtime_config: RuntimeConfig,
    runtime_state: RuntimeState,
) -> None:
    transport = MqttTransport(runtime_config, runtime_state)
    transport.enqueue_mqtt = AsyncMock()
    service = BridgeService(runtime_config, runtime_state, transport)

    sent_frames: list[tuple[int, bytes]] = []
    flow = service.serial_flow

    async def fake_sender(
        command_id: int, payload: bytes, seq_id: int | None = None
    ) -> bool:
        sent_frames.append((command_id, payload))
        flow.on_frame_received(
            Command.CMD_ANALOG_READ_RESP.value,
            0,
            bytes([0, 0]),
        )
        return True

    service.register_serial_sender(fake_sender)

    await service.handle_mqtt_message(
        Message(
            topic=topic_path(
                runtime_state.mqtt_topic_prefix,
                Topic.ANALOG,
                "2",
                "read",
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
async def test_mcu_digital_read_request_yields_not_implemented(
    runtime_config: RuntimeConfig,
    runtime_state: RuntimeState,
) -> None:
    transport = MqttTransport(runtime_config, runtime_state)
    transport.enqueue_mqtt = AsyncMock()
    service = BridgeService(runtime_config, runtime_state, transport)

    sent_frames: list[tuple[int, bytes]] = []

    async def fake_sender(
        command_id: int, payload: bytes, seq_id: int | None = None
    ) -> bool:
        sent_frames.append((command_id, payload))
        return True

    service.register_serial_sender(fake_sender)

    await service.handle_mcu_frame(
        Command.CMD_DIGITAL_READ.value,
        0,
        bytes([9]),
    )

    assert any(f[0] == Status.NOT_IMPLEMENTED.value for f in sent_frames)


@pytest.mark.asyncio
async def test_mcu_free_memory_response_enqueues_value(
    runtime_config: RuntimeConfig,
    runtime_state: RuntimeState,
) -> None:
    transport = MqttTransport(runtime_config, runtime_state)
    transport.enqueue_mqtt = AsyncMock()
    service = BridgeService(runtime_config, runtime_state, transport)

    sent_frames: list[tuple[int, bytes]] = []

    async def fake_sender(
        command_id: int, payload: bytes, seq_id: int | None = None
    ) -> bool:
        sent_frames.append((command_id, payload))
        return True

    service.register_serial_sender(fake_sender)

    payload = msgspec.msgpack.encode(structures.FreeMemoryResponsePacket(value=100))
    await service.handle_mcu_frame(Command.CMD_GET_FREE_MEMORY_RESP.value, 0, payload)

    assert transport.enqueue_mqtt.call_count >= 1
    queued = transport.enqueue_mqtt.call_args_list[-1][0][0]
    expected_topic = topic_path(
        runtime_state.mqtt_topic_prefix,
        Topic.SYSTEM,
        "free_memory",
        "value",
    )
    assert queued.topic_name == expected_topic
    assert queued.payload == b"100"
    # task done removed

    assert sent_frames
    ack_id, ack_payload = sent_frames[-1]
    assert ack_id == Status.ACK.value
    assert ack_payload == msgspec.msgpack.encode(
        structures.AckPacket(command_id=Command.CMD_GET_FREE_MEMORY_RESP.value)
    )


@pytest.mark.asyncio
async def test_mqtt_system_version_get_requests_and_publishes_cached(
    runtime_config: RuntimeConfig,
    runtime_state: RuntimeState,
) -> None:
    transport = MqttTransport(runtime_config, runtime_state)
    transport.enqueue_mqtt = AsyncMock()
    service = BridgeService(runtime_config, runtime_state, transport)

    runtime_state.mcu_version = (1, 2, 0)

    sent_frames: list[tuple[int, bytes]] = []
    flow = service.serial_flow

    async def fake_sender(
        command_id: int, payload: bytes, seq_id: int | None = None
    ) -> bool:
        sent_frames.append((command_id, payload))
        flow.on_frame_received(
            Command.CMD_GET_VERSION_RESP.value,
            0,
            bytes([1, 2]),
        )
        return True

    service.register_serial_sender(fake_sender)

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

    assert sent_frames
    assert sent_frames[0][0] == Command.CMD_GET_VERSION.value
    assert runtime_state.mcu_version is None

    assert transport.enqueue_mqtt.call_count >= 1
    queued = transport.enqueue_mqtt.call_args_list[-1][0][0]
    expected_topic = topic_path(
        runtime_state.mqtt_topic_prefix,
        Topic.SYSTEM,
        "version",
        "value",
    )
    assert queued.topic_name == expected_topic
    assert queued.payload == b"1.2.0"
    # task done removed


@pytest.mark.asyncio
async def test_mqtt_shell_kill_invokes_processonent(
    runtime_config: RuntimeConfig,
    runtime_state: RuntimeState,
) -> None:
    transport = MqttTransport(runtime_config, runtime_state)
    transport.enqueue_mqtt = AsyncMock()
    service = BridgeService(runtime_config, runtime_state, transport)
    process = service.process

    with patch.object(process, "handle_mqtt", new_callable=AsyncMock) as mock_mqtt:
        # Re-register mock in router because dispatcher registers methods at init time
        service.dispatcher.mqtt_handlers[Topic.SHELL] = mock_mqtt
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
