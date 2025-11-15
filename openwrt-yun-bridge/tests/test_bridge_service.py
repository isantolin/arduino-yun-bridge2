"""Unit tests for BridgeService lifecycle helpers."""
from __future__ import annotations

import asyncio
import logging
import struct

import pytest

from yunbridge.config.settings import RuntimeConfig
from yunbridge.services.runtime import BridgeService
from yunbridge.state.context import RuntimeState
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
        assert sent_frames[0][0] == Command.CMD_GET_VERSION.value
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
        frame_id, payload = sent_frames[-1]
        assert frame_id == Command.CMD_MAILBOX_AVAILABLE_RESP.value
        assert payload == b"\x02"

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
        assert len(send_attempts) == 2
        assert send_attempts[0][0] == Command.CMD_MAILBOX_READ_RESP.value
        assert send_attempts[1][0] == Command.CMD_MAILBOX_READ_RESP.value

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
