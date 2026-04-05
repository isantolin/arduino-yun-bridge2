"""Pin/system component integration tests for BridgeService."""

from __future__ import annotations

from unittest.mock import AsyncMock, patch

import pytest
from aiomqtt.message import Message
from mcubridge.config.settings import RuntimeConfig
from mcubridge.protocol import protocol, structures
from mcubridge.protocol.protocol import Command, Status
from mcubridge.protocol.topics import Topic, topic_path
from mcubridge.services.process import ProcessComponent
from mcubridge.services.runtime import BridgeService
from mcubridge.state.context import (
    PendingPinRequest,
    RuntimeState,
)

from .mqtt_helpers import make_inbound_message


def _make_inbound(
    topic: str,
    payload: bytes = b"",
    *,
    qos: int = 0,
    retain: bool = False,
) -> Message:
    return make_inbound_message(
        topic,
        payload,
        qos=qos,
        retain=retain,
    )


@pytest.mark.asyncio
async def test_mcu_digital_read_response_publishes_to_mqtt(
    runtime_config: RuntimeConfig,
    runtime_state: RuntimeState,
) -> None:
    service = BridgeService(runtime_config, runtime_state)

    sent_frames: list[tuple[int, bytes]] = []

    async def fake_sender(command_id: int, payload: bytes, seq_id: int | None = None) -> bool:
        sent_frames.append((command_id, payload))
        return True

    service.register_serial_sender(fake_sender)

    runtime_state.pending_digital_reads.append(PendingPinRequest(pin=7, reply_context=None))

    payload = structures.DigitalReadResponsePacket(value=1).encode()
    await service.handle_mcu_frame(Command.CMD_DIGITAL_READ_RESP.value, 0, payload)

    queued = runtime_state.mqtt_publish_queue.get_nowait()
    expected_topic = topic_path(
        runtime_state.mqtt_topic_prefix,
        Topic.DIGITAL,
        "7",
        "value",
    )
    assert queued.topic_name == expected_topic
    assert queued.payload == b"1"
    runtime_state.mqtt_publish_queue.task_done()

    assert sent_frames
    ack_id, ack_payload = sent_frames[-1]
    assert ack_id == Status.ACK.value
    assert ack_payload == structures.AckPacket(command_id=Command.CMD_DIGITAL_READ_RESP.value).encode()


@pytest.mark.asyncio
async def test_mcu_analog_read_response_publishes_to_mqtt(
    runtime_config: RuntimeConfig,
    runtime_state: RuntimeState,
) -> None:
    service = BridgeService(runtime_config, runtime_state)

    sent_frames: list[tuple[int, bytes]] = []

    async def fake_sender(command_id: int, payload: bytes, seq_id: int | None = None) -> bool:
        sent_frames.append((command_id, payload))
        return True

    service.register_serial_sender(fake_sender)

    runtime_state.pending_analog_reads.append(PendingPinRequest(pin=3, reply_context=None))

    TEST_EXIT_CODE = 0x7F
    payload = structures.AnalogReadResponsePacket(value=TEST_EXIT_CODE).encode()
    await service.handle_mcu_frame(Command.CMD_ANALOG_READ_RESP.value, 0, payload)

    queued = runtime_state.mqtt_publish_queue.get_nowait()
    expected_topic = topic_path(
        runtime_state.mqtt_topic_prefix,
        Topic.ANALOG,
        "3",
        "value",
    )
    assert queued.topic_name == expected_topic
    assert queued.payload == b"127"
    runtime_state.mqtt_publish_queue.task_done()

    assert sent_frames
    ack_id, ack_payload = sent_frames[-1]
    assert ack_id == Status.ACK.value
    assert ack_payload == structures.AckPacket(command_id=Command.CMD_ANALOG_READ_RESP.value).encode()


@pytest.mark.asyncio
async def test_mqtt_digital_write_sends_frame(
    runtime_config: RuntimeConfig,
    runtime_state: RuntimeState,
) -> None:
    service = BridgeService(runtime_config, runtime_state)

    sent_frames: list[tuple[int, bytes]] = []
    flow = service._serial_flow  # type: ignore[reportPrivateUsage]

    async def fake_sender(command_id: int, payload: bytes, seq_id: int | None = None) -> bool:
        sent_frames.append((command_id, payload))
        flow.on_frame_received(  # type: ignore[reportCallIssue]
            Status.ACK.value,
            structures.AckPacket(command_id=command_id).encode(),
        )
        return True

    service.register_serial_sender(fake_sender)

    await service.handle_mqtt_message(
        _make_inbound(
            topic_path(
                runtime_state.mqtt_topic_prefix,
                Topic.DIGITAL,
                "5",
            ),
            b"1",
        )
    )

    assert sent_frames
    command_id, payload = sent_frames[0]
    assert command_id == Command.CMD_DIGITAL_WRITE.value
    assert payload == structures.DigitalWritePacket.SCHEMA.build(dict(pin=5, value=protocol.DIGITAL_HIGH))


@pytest.mark.asyncio
async def test_mqtt_analog_read_tracks_pending_queue(
    runtime_config: RuntimeConfig,
    runtime_state: RuntimeState,
) -> None:
    service = BridgeService(runtime_config, runtime_state)

    sent_frames: list[tuple[int, bytes]] = []
    flow = service._serial_flow  # type: ignore[reportPrivateUsage]

    async def fake_sender(command_id: int, payload: bytes, seq_id: int | None = None) -> bool:
        sent_frames.append((command_id, payload))
        flow.on_frame_received(
            Command.CMD_ANALOG_READ_RESP.value, 0, bytes([0, 0]),
        )
        return True

    service.register_serial_sender(fake_sender)

    await service.handle_mqtt_message(
        _make_inbound(
            topic_path(
                runtime_state.mqtt_topic_prefix,
                Topic.ANALOG,
                "2",
                "read",
            ),
            b"",
        )
    )

    assert sent_frames
    command_id, payload = sent_frames[0]
    assert command_id == Command.CMD_ANALOG_READ.value
    assert payload == structures.PinReadPacket.SCHEMA.build({"pin": 2})
    pending = runtime_state.pending_analog_reads[-1]
    assert pending.pin == 2


@pytest.mark.asyncio
async def test_mcu_digital_read_request_yields_not_implemented(
    runtime_config: RuntimeConfig,
    runtime_state: RuntimeState,
) -> None:
    service = BridgeService(runtime_config, runtime_state)

    sent_frames: list[tuple[int, bytes]] = []

    async def fake_sender(command_id: int, payload: bytes, seq_id: int | None = None) -> bool:
        sent_frames.append((command_id, payload))
        return True

    service.register_serial_sender(fake_sender)

    await service.handle_mcu_frame(
        Command.CMD_DIGITAL_READ.value, 0, bytes([9]),
    )

    assert any(f[0] == Status.NOT_IMPLEMENTED.value for f in sent_frames)


@pytest.mark.asyncio
async def test_mcu_free_memory_response_enqueues_value(
    runtime_config: RuntimeConfig,
    runtime_state: RuntimeState,
) -> None:
    service = BridgeService(runtime_config, runtime_state)

    sent_frames: list[tuple[int, bytes]] = []

    async def fake_sender(command_id: int, payload: bytes, seq_id: int | None = None) -> bool:
        sent_frames.append((command_id, payload))
        return True

    service.register_serial_sender(fake_sender)

    payload = structures.FreeMemoryResponsePacket(value=100).encode()
    await service.handle_mcu_frame(Command.CMD_GET_FREE_MEMORY_RESP.value, 0, payload)

    queued = runtime_state.mqtt_publish_queue.get_nowait()
    expected_topic = topic_path(
        runtime_state.mqtt_topic_prefix,
        Topic.SYSTEM,
        "free_memory",
        "value",
    )
    assert queued.topic_name == expected_topic
    assert queued.payload == b"100"
    runtime_state.mqtt_publish_queue.task_done()

    assert sent_frames
    ack_id, ack_payload = sent_frames[-1]
    assert ack_id == Status.ACK.value
    assert ack_payload == structures.AckPacket(command_id=Command.CMD_GET_FREE_MEMORY_RESP.value).encode()


@pytest.mark.asyncio
async def test_mqtt_system_version_get_requests_and_publishes_cached(
    runtime_config: RuntimeConfig,
    runtime_state: RuntimeState,
) -> None:
    service = BridgeService(runtime_config, runtime_state)

    runtime_state.mcu_version = (1, 2)

    sent_frames: list[tuple[int, bytes]] = []
    flow = service._serial_flow  # type: ignore[reportPrivateUsage]

    async def fake_sender(command_id: int, payload: bytes, seq_id: int | None = None) -> bool:
        sent_frames.append((command_id, payload))
        flow.on_frame_received(
            Command.CMD_GET_VERSION_RESP.value, 0, bytes([1, 2]),
        )
        return True

    service.register_serial_sender(fake_sender)

    await service.handle_mqtt_message(
        _make_inbound(
            topic_path(
                runtime_state.mqtt_topic_prefix,
                Topic.SYSTEM,
                "version",
                "get",
            ),
            b"",
        )
    )

    assert sent_frames
    assert sent_frames[0][0] == Command.CMD_GET_VERSION.value
    assert runtime_state.mcu_version is None

    queued = runtime_state.mqtt_publish_queue.get_nowait()
    expected_topic = topic_path(
        runtime_state.mqtt_topic_prefix,
        Topic.SYSTEM,
        "version",
        "value",
    )
    assert queued.topic_name == expected_topic
    assert queued.payload == b"1.2"
    runtime_state.mqtt_publish_queue.task_done()


@pytest.mark.asyncio
async def test_mqtt_shell_kill_invokes_processonent(
    runtime_config: RuntimeConfig,
    runtime_state: RuntimeState,
) -> None:
    service = BridgeService(runtime_config, runtime_state)
    process = service._container.get(ProcessComponent)  # type: ignore[reportPrivateUsage]

    with patch.object(process, "handle_mqtt", new_callable=AsyncMock) as mock_mqtt:
        pid = 21
        await service.handle_mqtt_message(
            _make_inbound(
                topic_path(
                    runtime_state.mqtt_topic_prefix,
                    Topic.SHELL,
                    "kill",
                    str(pid),
                ),
                b"",
            )
        )
        # ProcessComponent handles shell topics
        mock_mqtt.assert_called()
        args = mock_mqtt.call_args[0]
        # args[0] is segments, args[1] is payload
        assert args[0] == ["kill", str(pid)]

