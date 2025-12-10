"""Pin/system component integration tests for BridgeService."""
from __future__ import annotations

import asyncio
import struct
from unittest.mock import patch

from aiomqtt.message import Message as MQTTMessage
from yunbridge.rpc.protocol import Command, Status

from yunbridge.common import encode_status_reason
from yunbridge.config.settings import RuntimeConfig
from yunbridge.protocol.topics import Topic, topic_path
from yunbridge.services.runtime import BridgeService
from yunbridge.state.context import (
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
) -> MQTTMessage:
    return make_inbound_message(
        topic,
        payload,
        qos=qos,
        retain=retain,
    )


def test_mcu_digital_read_response_publishes_to_mqtt(
    runtime_config: RuntimeConfig,
    runtime_state: RuntimeState,
) -> None:
    async def _run() -> None:
        service = BridgeService(runtime_config, runtime_state)

        sent_frames: list[tuple[int, bytes]] = []

        async def fake_sender(command_id: int, payload: bytes) -> bool:
            sent_frames.append((command_id, payload))
            return True

        service.register_serial_sender(fake_sender)

        runtime_state.pending_digital_reads.append(
            PendingPinRequest(pin=7, reply_context=None)
        )

        await service.handle_mcu_frame(
            Command.CMD_DIGITAL_READ_RESP.value,
            bytes([1]),
        )

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
        assert ack_payload == struct.pack(
            ">H", Command.CMD_DIGITAL_READ_RESP.value
        )

    asyncio.run(_run())


def test_mcu_analog_read_response_publishes_to_mqtt(
    runtime_config: RuntimeConfig,
    runtime_state: RuntimeState,
) -> None:
    async def _run() -> None:
        service = BridgeService(runtime_config, runtime_state)

        sent_frames: list[tuple[int, bytes]] = []

        async def fake_sender(command_id: int, payload: bytes) -> bool:
            sent_frames.append((command_id, payload))
            return True

        service.register_serial_sender(fake_sender)

        runtime_state.pending_analog_reads.append(
            PendingPinRequest(pin=3, reply_context=None)
        )

        await service.handle_mcu_frame(
            Command.CMD_ANALOG_READ_RESP.value,
            bytes([0, 0x7F]),
        )

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
        assert ack_payload == struct.pack(
            ">H", Command.CMD_ANALOG_READ_RESP.value
        )

    asyncio.run(_run())


def test_mqtt_digital_write_sends_frame(
    runtime_config: RuntimeConfig,
    runtime_state: RuntimeState,
) -> None:
    async def _run() -> None:
        service = BridgeService(runtime_config, runtime_state)

        sent_frames: list[tuple[int, bytes]] = []
        flow = service._serial_flow  # pyright: ignore[reportPrivateUsage]

        async def fake_sender(command_id: int, payload: bytes) -> bool:
            sent_frames.append((command_id, payload))
            flow.on_frame_received(
                Status.ACK.value,
                struct.pack(">H", command_id),
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
        assert payload == struct.pack(">BB", 5, 1)

    asyncio.run(_run())


def test_mqtt_analog_read_tracks_pending_queue(
    runtime_config: RuntimeConfig,
    runtime_state: RuntimeState,
) -> None:
    async def _run() -> None:
        service = BridgeService(runtime_config, runtime_state)

        sent_frames: list[tuple[int, bytes]] = []
        flow = service._serial_flow  # pyright: ignore[reportPrivateUsage]

        async def fake_sender(command_id: int, payload: bytes) -> bool:
            sent_frames.append((command_id, payload))
            flow.on_frame_received(
                Command.CMD_ANALOG_READ_RESP.value,
                bytes([0, 0]),
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
        assert payload == struct.pack(">B", 2)
        pending = runtime_state.pending_analog_reads[-1]
        assert pending.pin == 2

    asyncio.run(_run())


def test_mcu_digital_read_request_yields_not_implemented(
    runtime_config: RuntimeConfig,
    runtime_state: RuntimeState,
) -> None:
    async def _run() -> None:
        service = BridgeService(runtime_config, runtime_state)

        sent_frames: list[tuple[int, bytes]] = []

        async def fake_sender(command_id: int, payload: bytes) -> bool:
            sent_frames.append((command_id, payload))
            return True

        service.register_serial_sender(fake_sender)

        await service.handle_mcu_frame(
            Command.CMD_DIGITAL_READ.value,
            bytes([9]),
        )

        assert sent_frames == [
            (
                Status.NOT_IMPLEMENTED.value,
                encode_status_reason(
                    "pin-read-origin-mcu:linux_gpio_read_not_available"
                ),
            )
        ]

    asyncio.run(_run())


def test_mcu_free_memory_response_enqueues_value(
    runtime_config: RuntimeConfig,
    runtime_state: RuntimeState,
) -> None:
    async def _run() -> None:
        service = BridgeService(runtime_config, runtime_state)

        sent_frames: list[tuple[int, bytes]] = []

        async def fake_sender(command_id: int, payload: bytes) -> bool:
            sent_frames.append((command_id, payload))
            return True

        service.register_serial_sender(fake_sender)

        await service.handle_mcu_frame(
            Command.CMD_GET_FREE_MEMORY_RESP.value,
            bytes([0, 100]),
        )

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
        assert ack_payload == struct.pack(
            ">H", Command.CMD_GET_FREE_MEMORY_RESP.value
        )

    asyncio.run(_run())


def test_mqtt_system_version_get_requests_and_publishes_cached(
    runtime_config: RuntimeConfig,
    runtime_state: RuntimeState,
) -> None:
    async def _run() -> None:
        service = BridgeService(runtime_config, runtime_state)

        runtime_state.mcu_version = (1, 2)

        sent_frames: list[tuple[int, bytes]] = []
        flow = service._serial_flow  # pyright: ignore[reportPrivateUsage]

        async def fake_sender(command_id: int, payload: bytes) -> bool:
            sent_frames.append((command_id, payload))
            flow.on_frame_received(
                Command.CMD_GET_VERSION_RESP.value,
                bytes([1, 2]),
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

    asyncio.run(_run())


def test_mqtt_shell_run_publishes_response(
    runtime_config: RuntimeConfig,
    runtime_state: RuntimeState,
) -> None:
    async def _run() -> None:
        service = BridgeService(runtime_config, runtime_state)

        calls: list[tuple[object, str]] = []

        async def fake_run(
            self: object, command: str
        ) -> tuple[int, bytes, bytes, int | None]:
            calls.append((self, command))
            return Status.OK.value, b"ok\n", b"", 0

        with patch(
            "yunbridge.services.components.process.ProcessComponent.run_sync",
            new=fake_run,
        ):
            await service.handle_mqtt_message(
                _make_inbound(
                    topic_path(
                        runtime_state.mqtt_topic_prefix,
                        Topic.SHELL,
                        "run",
                    ),
                    b"echo test",
                )
            )
            assert calls == [
                (
                    service._process,  # pyright: ignore[reportPrivateUsage]
                    "echo test",
                )
            ]

        queued = runtime_state.mqtt_publish_queue.get_nowait()
        expected_topic = topic_path(
            runtime_state.mqtt_topic_prefix,
            Topic.SHELL,
            "response",
        )
        assert queued.topic_name == expected_topic
        assert b"Exit Code: 0" in queued.payload
        runtime_state.mqtt_publish_queue.task_done()

    asyncio.run(_run())


def test_mqtt_shell_run_async_handles_not_allowed(
    runtime_config: RuntimeConfig,
    runtime_state: RuntimeState,
) -> None:
    async def _run() -> None:
        service = BridgeService(runtime_config, runtime_state)

        calls: list[tuple[object, str]] = []

        async def fake_start(self: object, command: str) -> int:
            calls.append((self, command))
            return 0xFFFF

        with patch(
            "yunbridge.services.components.process."
            "ProcessComponent.start_async",
            new=fake_start,
        ):
            await service.handle_mqtt_message(
                _make_inbound(
                    topic_path(
                        runtime_state.mqtt_topic_prefix,
                        Topic.SHELL,
                        "run_async",
                    ),
                    b"blocked",
                )
            )
            assert calls == [
                (
                    service._process,  # pyright: ignore[reportPrivateUsage]
                    "blocked",
                )
            ]

        queued = runtime_state.mqtt_publish_queue.get_nowait()
        expected_topic = topic_path(
            runtime_state.mqtt_topic_prefix,
            Topic.SHELL,
            "run_async",
            "response",
        )
        assert queued.topic_name == expected_topic
        assert queued.payload == b"error:not_allowed"
        runtime_state.mqtt_publish_queue.task_done()

    asyncio.run(_run())


def test_mqtt_shell_kill_invokes_process_component(
    runtime_config: RuntimeConfig,
    runtime_state: RuntimeState,
) -> None:
    async def _run() -> None:
        service = BridgeService(runtime_config, runtime_state)

        calls: list[tuple[object, bytes, bool]] = []

        async def fake_kill(
            self: object,
            payload: bytes,
            *,
            send_ack: bool,
        ) -> bool:
            calls.append((self, payload, send_ack))
            return True

        with patch(
            "yunbridge.services.components.process."
            "ProcessComponent.handle_kill",
            new=fake_kill,
        ):
            await service.handle_mqtt_message(
                _make_inbound(
                    topic_path(
                        runtime_state.mqtt_topic_prefix,
                        Topic.SHELL,
                        "kill",
                        "21",
                    ),
                    b"",
                )
            )
            assert calls == [
                (
                    service._process,  # pyright: ignore[reportPrivateUsage]
                    struct.pack(">H", 21),
                    False,
                )
            ]

    asyncio.run(_run())
