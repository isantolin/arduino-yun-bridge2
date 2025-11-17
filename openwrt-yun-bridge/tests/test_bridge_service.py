"""Unit tests for BridgeService lifecycle helpers."""
from __future__ import annotations

import asyncio
import logging
import struct

import pytest

from yunbridge.config.settings import RuntimeConfig
from yunbridge.services.runtime import BridgeService
from yunbridge.state.context import RuntimeState
from yunbridge.mqtt import PublishableMessage
from yunrpc.protocol import Command, Status


def test_on_serial_connected_flushes_console_queue(
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

        runtime_state.enqueue_console_chunk(b"hello", logging.getLogger())
        runtime_state.mcu_is_paused = False
        runtime_state.mcu_version = (1, 2)

        await service.on_serial_connected()

        assert sent_frames
        frame_ids = [frame_id for frame_id, _ in sent_frames]
        assert frame_ids[:2] == [
            Command.CMD_LINK_RESET.value,
            Command.CMD_LINK_SYNC.value,
        ]
        assert Command.CMD_GET_VERSION.value in frame_ids
        assert any(
            frame_id == Command.CMD_CONSOLE_WRITE.value
            for frame_id, _ in sent_frames
        )
        assert runtime_state.console_queue_bytes == 0
        assert runtime_state.mcu_version is None

    asyncio.run(_run())


def test_mailbox_available_flow(
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

        runtime_state.mailbox_queue.append(b"msg1")
        runtime_state.mailbox_queue.append(b"msg2")

        await service.handle_mcu_frame(
            Command.CMD_MAILBOX_AVAILABLE.value,
            b"",
        )

        assert sent_frames
        frame_ids = [frame_id for frame_id, _ in sent_frames]
        assert frame_ids[-2] == Command.CMD_MAILBOX_AVAILABLE_RESP.value
        assert sent_frames[-2][1] == b"\x02"
        # Final frame should be ACK referencing the original command.
        assert frame_ids[-1] == Status.ACK.value
        assert sent_frames[-1][1] == struct.pack(
            ">H", Command.CMD_MAILBOX_AVAILABLE.value
        )

    asyncio.run(_run())


def test_mailbox_push_overflow_returns_error(
    runtime_config: RuntimeConfig,
    runtime_state: RuntimeState,
) -> None:
    async def _run() -> None:
        service = BridgeService(runtime_config, runtime_state)
        runtime_state.mailbox_queue_limit = 0

        sent_frames: list[tuple[int, bytes]] = []

        async def fake_sender(command_id: int, payload: bytes) -> bool:
            sent_frames.append((command_id, payload))
            return True

        service.register_serial_sender(fake_sender)

        payload = struct.pack(">H", 3) + b"abc"
        await service.handle_mcu_frame(Command.CMD_MAILBOX_PUSH.value, payload)

        assert runtime_state.mailbox_incoming_queue_bytes == 0
        assert runtime_state.mqtt_publish_queue.qsize() == 0
        assert sent_frames
        status_id, status_payload = sent_frames[-1]
        assert status_id == Status.ERROR.value
        assert status_payload == b"mailbox_incoming_overflow"

    asyncio.run(_run())


def test_mailbox_read_requeues_on_send_failure(
    runtime_config: RuntimeConfig,
    runtime_state: RuntimeState,
) -> None:
    async def _run() -> None:
        service = BridgeService(runtime_config, runtime_state)

        logger = logging.getLogger("test.mailbox")
        stored = runtime_state.enqueue_mailbox_message(b"payload", logger)
        assert stored
        original_bytes = runtime_state.mailbox_queue_bytes

        send_results = [False, True]
        send_attempts: list[tuple[int, bytes]] = []

        async def fake_sender(command_id: int, payload: bytes) -> bool:
            send_attempts.append((command_id, payload))
            return send_results.pop(0)

        service.register_serial_sender(fake_sender)

        await service.handle_mcu_frame(Command.CMD_MAILBOX_READ.value, b"")

        assert runtime_state.mailbox_queue
        assert runtime_state.mailbox_queue_bytes == original_bytes
        assert runtime_state.mqtt_publish_queue.qsize() == 0

        await service.handle_mcu_frame(Command.CMD_MAILBOX_READ.value, b"")

        assert not runtime_state.mailbox_queue
        assert runtime_state.mailbox_queue_bytes == 0
        assert runtime_state.mqtt_publish_queue.qsize() == 1
        assert len(send_attempts) == 3
        assert send_attempts[0][0] == Command.CMD_MAILBOX_READ_RESP.value
        assert send_attempts[1][0] == Command.CMD_MAILBOX_READ_RESP.value
        # Final send is the ACK covering the MCU command.
        assert send_attempts[2][0] == Status.ACK.value
        assert send_attempts[2][1] == struct.pack(
            ">H", Command.CMD_MAILBOX_READ.value
        )

    asyncio.run(_run())


def test_on_serial_disconnected_clears_pending(
    runtime_config: RuntimeConfig,
    runtime_state: RuntimeState,
    caplog: pytest.LogCaptureFixture,
) -> None:
    async def _run() -> None:
        service = BridgeService(runtime_config, runtime_state)

        runtime_state.pending_digital_reads.extend([1, 2])
        runtime_state.pending_analog_reads.append(3)
        runtime_state.pending_datastore_gets.append("key")
        runtime_state.mcu_is_paused = True
        runtime_state.enqueue_console_chunk(b"keep", logging.getLogger())

        with caplog.at_level(logging.WARNING, logger="yunbridge.service"):
            await service.on_serial_disconnected()

        assert not runtime_state.pending_digital_reads
        assert not runtime_state.pending_analog_reads
        assert not runtime_state.pending_datastore_gets
        assert runtime_state.mcu_is_paused is False
        assert runtime_state.console_to_mcu_queue
        assert runtime_state.console_to_mcu_queue[0] == b"keep"
        assert runtime_state.console_queue_bytes == len(
            runtime_state.console_to_mcu_queue[0]
        )
        assert any("clearing" in record.message for record in caplog.records)

    asyncio.run(_run())


def test_mqtt_mailbox_read_preserves_empty_payload(
    runtime_config: RuntimeConfig,
    runtime_state: RuntimeState,
) -> None:
    async def _run() -> None:
        service = BridgeService(runtime_config, runtime_state)

        logger = logging.getLogger("test.mailbox.empty")
        stored = runtime_state.enqueue_mailbox_incoming(b"", logger)
        assert stored

        await service.handle_mqtt_message(
            f"{runtime_state.mqtt_topic_prefix}/mailbox/read", b""
        )

        assert runtime_state.mqtt_publish_queue.qsize() == 2
        topic_payloads = [
            runtime_state.mqtt_publish_queue.get_nowait()
            for _ in range(2)
        ]
        # First message is the payload, second is the availability update.
        assert topic_payloads[0].topic_name.endswith("/mailbox/incoming")
        assert topic_payloads[0].payload == b""
        assert topic_payloads[1].topic_name.endswith(
            "/mailbox/incoming_available"
        )
        for _ in topic_payloads:
            runtime_state.mqtt_publish_queue.task_done()

    asyncio.run(_run())


def test_enqueue_mqtt_drops_oldest_when_full(
    runtime_config: RuntimeConfig,
    runtime_state: RuntimeState,
) -> None:
    async def _run() -> None:
        runtime_state.mqtt_publish_queue = asyncio.Queue(maxsize=1)
        runtime_state.mqtt_queue_limit = 1
        service = BridgeService(runtime_config, runtime_state)

        first = PublishableMessage("br/test/one", b"1")
        second = PublishableMessage("br/test/two", b"2")

        await service.enqueue_mqtt(first)
        await service.enqueue_mqtt(second)

        assert runtime_state.mqtt_dropped_messages == 1
        assert runtime_state.mqtt_drop_counts.get("br/test/one") == 1
        assert runtime_state.mqtt_publish_queue.qsize() == 1

        queued = runtime_state.mqtt_publish_queue.get_nowait()
        assert queued.topic_name == "br/test/two"
        runtime_state.mqtt_publish_queue.task_done()

    asyncio.run(_run())
