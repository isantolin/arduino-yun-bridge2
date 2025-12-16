"""Unit tests for BridgeService lifecycle helpers."""

from __future__ import annotations

import asyncio
import json
import logging
import struct
from unittest.mock import patch

import pytest
from aiomqtt.message import Message as MQTTMessage

from yunbridge.config.settings import RuntimeConfig
from yunbridge.policy import AllowedCommandPolicy, TopicAuthorization
from yunbridge.protocol.topics import (
    Topic,
    mailbox_incoming_available_topic,
    topic_path,
)
from yunbridge.services.runtime import BridgeService, SerialHandshakeFatal
from yunbridge.state.context import (
    PendingPinRequest,
    RuntimeState,
)
from yunbridge.mqtt.messages import QueuedPublish
from yunbridge.const import (
    SERIAL_HANDSHAKE_BACKOFF_BASE,
    SERIAL_NONCE_LENGTH,
)
from yunbridge.rpc import protocol as rpc_protocol
from yunbridge.rpc.protocol import Command, Status
from yunbridge.services.components.process import ProcessComponent
from yunbridge.services.handshake import derive_serial_timing
from .mqtt_helpers import make_inbound_message


class _FakeMonotonic:
    def __init__(self, start: float = 0.0) -> None:
        self.value = start

    def monotonic(self) -> float:
        return self.value

    def advance(self, delta: float) -> None:
        self.value += delta


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


def test_on_serial_connected_flushes_console_queue(
    runtime_config: RuntimeConfig,
    runtime_state: RuntimeState,
) -> None:
    async def _run() -> None:
        service = BridgeService(runtime_config, runtime_state)

        sent_frames: list[tuple[int, bytes]] = []

        flow = service._serial_flow  # pyright: ignore[reportPrivateUsage]

        async def fake_sender(command_id: int, payload: bytes) -> bool:
            sent_frames.append((command_id, payload))
            if command_id == Command.CMD_LINK_RESET.value:
                await service.handle_mcu_frame(
                    Command.CMD_LINK_RESET_RESP.value,
                    b"",
                )
            elif command_id == Command.CMD_LINK_SYNC.value:
                nonce = service.state.link_handshake_nonce or b""
                tag = service._compute_handshake_tag(nonce)
                response = nonce + tag
                await service.handle_mcu_frame(
                    Command.CMD_LINK_SYNC_RESP.value,
                    response,
                )
            elif command_id == Command.CMD_GET_VERSION.value:
                flow.on_frame_received(
                    Command.CMD_GET_VERSION_RESP.value,
                    b"\x01\x02",
                )
            elif command_id == Command.CMD_CONSOLE_WRITE.value:
                flow.on_frame_received(
                    Status.ACK.value,
                    struct.pack(rpc_protocol.UINT16_FORMAT, Command.CMD_CONSOLE_WRITE.value),
                )
            return True

        service.register_serial_sender(fake_sender)

        runtime_state.enqueue_console_chunk(b"hello", logging.getLogger())
        runtime_state.mcu_is_paused = False
        runtime_state.mcu_version = (1, 2)

        await service.on_serial_connected()

        assert sent_frames
        reset_payloads = [
            payload
            for frame_id, payload in sent_frames
            if frame_id == Command.CMD_LINK_RESET.value
        ]
        assert reset_payloads
        reset_payload = reset_payloads[0]
        assert len(reset_payload) == rpc_protocol.HANDSHAKE_CONFIG_SIZE
        timing = derive_serial_timing(runtime_config)
        unpacked = struct.unpack(
            rpc_protocol.HANDSHAKE_CONFIG_FORMAT,
            reset_payload,
        )
        assert unpacked[0] == timing.ack_timeout_ms
        assert unpacked[1] == timing.retry_limit
        assert unpacked[2] == timing.response_timeout_ms
        frame_ids = [frame_id for frame_id, _ in sent_frames]
        handshake_ids = [
            frame_id
            for frame_id in frame_ids
            if frame_id
            in {
                Command.CMD_LINK_RESET.value,
                Command.CMD_LINK_SYNC.value,
            }
        ]
        assert handshake_ids[:2] == [
            Command.CMD_LINK_RESET.value,
            Command.CMD_LINK_SYNC.value,
        ]
        assert Command.CMD_GET_VERSION.value in frame_ids
        assert any(
            frame_id == Command.CMD_CONSOLE_WRITE.value for frame_id, _ in sent_frames
        )
        assert runtime_state.console_queue_bytes == 0
        assert runtime_state.mcu_version is None
        assert runtime_state.handshake_attempts == 1
        assert runtime_state.handshake_successes == 1
        assert runtime_state.handshake_failures == 0
        assert runtime_state.serial_link_connected is True
        assert runtime_state.serial_ack_timeout_ms == timing.ack_timeout_ms
        assert runtime_state.serial_response_timeout_ms == timing.response_timeout_ms
        assert runtime_state.serial_retry_limit == timing.retry_limit

    asyncio.run(_run())


def test_sync_link_rejects_invalid_handshake_tag(
    runtime_config: RuntimeConfig, runtime_state: RuntimeState
) -> None:
    async def _run() -> None:
        service = BridgeService(runtime_config, runtime_state)

        sent_frames: list[tuple[int, bytes]] = []

        async def fake_sender(command_id: int, payload: bytes) -> bool:
            sent_frames.append((command_id, payload))
            if command_id == Command.CMD_LINK_RESET.value:
                await service.handle_mcu_frame(
                    Command.CMD_LINK_RESET_RESP.value,
                    b"",
                )
            elif command_id == Command.CMD_LINK_SYNC.value:
                nonce = service.state.link_handshake_nonce or b""
                tag = bytearray(service._compute_handshake_tag(nonce))
                if tag:
                    tag[0] ^= 0xFF
                response = nonce + bytes(tag)
                await service.handle_mcu_frame(
                    Command.CMD_LINK_SYNC_RESP.value,
                    response,
                )
            return True

        service.register_serial_sender(fake_sender)

        success = await service.sync_link()

        assert success is False
        assert service.state.link_is_synchronized is False
        assert service.state.link_handshake_nonce is None
        assert any(frame_id == Status.MALFORMED.value for frame_id, _ in sent_frames)
        assert runtime_state.handshake_attempts == 1
        assert runtime_state.handshake_failures == 1
        assert runtime_state.handshake_successes == 0
        assert runtime_state.handshake_fatal_count == 1
        assert runtime_state.handshake_fatal_reason == "sync_auth_mismatch"

    asyncio.run(_run())


def test_sync_link_rejects_truncated_response(
    runtime_config: RuntimeConfig, runtime_state: RuntimeState
) -> None:
    async def _run() -> None:
        service = BridgeService(runtime_config, runtime_state)

        sent_frames: list[tuple[int, bytes]] = []

        async def fake_sender(command_id: int, payload: bytes) -> bool:
            sent_frames.append((command_id, payload))
            if command_id == Command.CMD_LINK_RESET.value:
                await service.handle_mcu_frame(
                    Command.CMD_LINK_RESET_RESP.value,
                    b"",
                )
            elif command_id == Command.CMD_LINK_SYNC.value:
                nonce = service.state.link_handshake_nonce or b""
                await service.handle_mcu_frame(
                    Command.CMD_LINK_SYNC_RESP.value,
                    nonce,
                )
            return True

        service.register_serial_sender(fake_sender)

        success = await service.sync_link()

        assert success is False
        assert service.state.link_is_synchronized is False
        assert service.state.link_handshake_nonce is None
        assert any(frame_id == Status.MALFORMED.value for frame_id, _ in sent_frames)
        assert runtime_state.handshake_attempts == 1
        assert runtime_state.handshake_failures == 1
        assert runtime_state.handshake_fatal_count == 1
        assert runtime_state.handshake_fatal_reason == "sync_length_mismatch"

    asyncio.run(_run())


def test_repeated_sync_timeouts_become_fatal(
    runtime_config: RuntimeConfig,
    runtime_state: RuntimeState,
) -> None:
    async def _run() -> None:
        runtime_config.serial_handshake_fatal_failures = 2
        service = BridgeService(runtime_config, runtime_state)

        await service._handle_handshake_failure("link_sync_timeout")
        assert runtime_state.handshake_fatal_count == 0
        assert runtime_state.handshake_failure_streak == 1

        await service._handle_handshake_failure("link_sync_timeout")
        assert runtime_state.handshake_fatal_count == 1
        assert runtime_state.handshake_fatal_reason == "link_sync_timeout"
        assert runtime_state.handshake_fatal_detail == ("failure_streak_exceeded_2")

    asyncio.run(_run())


def test_link_sync_resp_respects_rate_limit(
    runtime_config: RuntimeConfig,
    runtime_state: RuntimeState,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def _run() -> None:
        runtime_config.serial_handshake_min_interval = 5.0
        service = BridgeService(runtime_config, runtime_state)

        sent_frames: list[tuple[int, bytes]] = []

        async def fake_sender(command_id: int, payload: bytes) -> bool:
            sent_frames.append((command_id, payload))
            return True

        service.register_serial_sender(fake_sender)

        fake_clock = _FakeMonotonic(100.0)
        monkeypatch.setattr(
            "yunbridge.services.handshake.time.monotonic",
            fake_clock.monotonic,
        )

        def _prime_handshake(seed: int) -> bytes:
            nonce = bytes([seed]) * SERIAL_NONCE_LENGTH
            tag = service._compute_handshake_tag(nonce)
            runtime_state.link_is_synchronized = False
            runtime_state.link_handshake_nonce = nonce
            runtime_state.link_nonce_length = len(nonce)
            runtime_state.link_expected_tag = tag
            return nonce + tag

        payload_ok = _prime_handshake(1)
        result_ok = await service._handle_link_sync_resp(payload_ok)
        assert result_ok is True

        fake_clock.advance(0.1)
        payload_blocked = _prime_handshake(2)
        rate_limited = await service._handle_link_sync_resp(payload_blocked)
        assert rate_limited is False
        assert runtime_state.last_handshake_error == "sync_rate_limited"
        assert runtime_state.handshake_failure_streak >= 1
        assert any(frame_id == Status.MALFORMED.value for frame_id, _ in sent_frames)

    asyncio.run(_run())


def test_sync_auth_failure_schedules_backoff(
    runtime_config: RuntimeConfig,
    runtime_state: RuntimeState,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def _run() -> None:
        service = BridgeService(runtime_config, runtime_state)

        async def fake_sender(command_id: int, payload: bytes) -> bool:
            return True

        service.register_serial_sender(fake_sender)

        fake_clock = _FakeMonotonic(200.0)
        monkeypatch.setattr(
            "yunbridge.services.handshake.time.monotonic",
            fake_clock.monotonic,
        )

        def _prime_handshake(seed: int) -> tuple[bytes, bytes]:
            nonce = bytes([seed]) * SERIAL_NONCE_LENGTH
            tag = service._compute_handshake_tag(nonce)
            runtime_state.link_is_synchronized = False
            runtime_state.link_handshake_nonce = nonce
            runtime_state.link_nonce_length = len(nonce)
            runtime_state.link_expected_tag = tag
            return nonce, tag

        nonce_one, tag_one = _prime_handshake(3)
        broken_tag_one = bytearray(tag_one)
        broken_tag_one[0] ^= 0xFF
        await service._handle_link_sync_resp(nonce_one + bytes(broken_tag_one))
        first_delay = runtime_state.handshake_backoff_until - fake_clock.monotonic()
        assert first_delay > 0
        assert runtime_state.last_handshake_error == "sync_auth_mismatch"
        assert runtime_state.handshake_fatal_count == 1
        assert runtime_state.handshake_fatal_reason == "sync_auth_mismatch"
        assert runtime_state.handshake_fatal_detail == "nonce_or_tag_mismatch"
        assert runtime_state.handshake_fatal_unix > 0

        fake_clock.advance(first_delay + 0.5)
        nonce_two, tag_two = _prime_handshake(4)
        broken_tag_two = bytearray(tag_two)
        broken_tag_two[-1] ^= 0xFF
        await service._handle_link_sync_resp(nonce_two + bytes(broken_tag_two))
        second_delay = runtime_state.handshake_backoff_until - fake_clock.monotonic()
        assert second_delay > first_delay
        assert runtime_state.handshake_failure_streak >= 2
        assert runtime_state.handshake_fatal_count == 2

    asyncio.run(_run())


@pytest.mark.asyncio
async def test_transient_handshake_failures_eventually_backoff(
    runtime_config: RuntimeConfig,
    runtime_state: RuntimeState,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    runtime_state.link_is_synchronized = False
    service = BridgeService(runtime_config, runtime_state)

    fake_clock = _FakeMonotonic(50.0)
    monkeypatch.setattr(
        "yunbridge.services.handshake.time.monotonic",
        fake_clock.monotonic,
    )

    for attempt in range(1, 4):
        await service._handle_handshake_failure("link_sync_timeout")
        if attempt < 3:
            assert runtime_state.handshake_backoff_until == 0
        else:
            remaining = runtime_state.handshake_backoff_until - fake_clock.monotonic()
            assert remaining >= SERIAL_HANDSHAKE_BACKOFF_BASE

    assert runtime_state.handshake_failure_streak == 3
    assert runtime_state.handshake_fatal_count == 1
    assert runtime_state.handshake_fatal_reason == "link_sync_timeout"
    assert runtime_state.handshake_fatal_detail == ("failure_streak_exceeded_3")


def test_derive_serial_timing_clamps_to_spec(
    runtime_config: RuntimeConfig,
) -> None:
    runtime_config.serial_retry_timeout = 0.0001
    runtime_config.serial_response_timeout = 999.0
    runtime_config.serial_retry_attempts = 99
    timing = derive_serial_timing(runtime_config)
    assert timing.ack_timeout_ms == rpc_protocol.HANDSHAKE_ACK_TIMEOUT_MIN_MS
    assert timing.response_timeout_ms == rpc_protocol.HANDSHAKE_RESPONSE_TIMEOUT_MAX_MS
    assert timing.retry_limit == rpc_protocol.HANDSHAKE_RETRY_LIMIT_MAX


def test_on_serial_connected_raises_on_secret_mismatch(
    runtime_config: RuntimeConfig,
    runtime_state: RuntimeState,
) -> None:
    async def _run() -> None:
        service = BridgeService(runtime_config, runtime_state)

        async def fake_sender(command_id: int, payload: bytes) -> bool:
            if command_id == Command.CMD_LINK_RESET.value:
                await service.handle_mcu_frame(
                    Command.CMD_LINK_RESET_RESP.value,
                    b"",
                )
            elif command_id == Command.CMD_LINK_SYNC.value:
                nonce = service.state.link_handshake_nonce or b""
                tag = bytearray(service._compute_handshake_tag(nonce))
                if tag:
                    tag[0] ^= 0xFF
                await service.handle_mcu_frame(
                    Command.CMD_LINK_SYNC_RESP.value,
                    nonce + bytes(tag),
                )
            return True

        service.register_serial_sender(fake_sender)

        with pytest.raises(SerialHandshakeFatal) as exc_info:
            await service.on_serial_connected()

        message = str(exc_info.value)
        assert "serial shared secret" in message
        assert "YUNBRIDGE_SERIAL_SECRET" in message

    asyncio.run(_run())


def test_mcu_status_frames_increment_counters(
    runtime_config: RuntimeConfig,
    runtime_state: RuntimeState,
) -> None:
    async def _run() -> None:
        service = BridgeService(runtime_config, runtime_state)
        runtime_state.link_is_synchronized = True

        await service.handle_mcu_frame(Status.ERROR.value, b"boom")

        assert runtime_state.mcu_status_counters["ERROR"] == 1

    asyncio.run(_run())


def test_mcu_frame_before_sync_is_rejected(
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

        runtime_state.link_is_synchronized = False

        await service.handle_mcu_frame(
            Command.CMD_MAILBOX_PUSH.value,
            b"\x00\x00",
        )

        assert sent_frames
        status_id, status_payload = sent_frames[-1]
        assert status_id == Status.MALFORMED.value
        assert status_payload[:2] == struct.pack(
            rpc_protocol.UINT16_FORMAT,
            Command.CMD_MAILBOX_PUSH.value,
        )
        assert runtime_state.mailbox_incoming_queue_bytes == 0

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
            rpc_protocol.UINT16_FORMAT, Command.CMD_MAILBOX_AVAILABLE.value
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

        payload = struct.pack(rpc_protocol.UINT16_FORMAT, 3) + b"abc"
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
        assert send_attempts[2][1] == struct.pack(rpc_protocol.UINT16_FORMAT, Command.CMD_MAILBOX_READ.value)

    asyncio.run(_run())


def test_datastore_get_from_mcu_returns_cached_value(
    runtime_config: RuntimeConfig,
    runtime_state: RuntimeState,
) -> None:
    async def _run() -> None:
        service = BridgeService(runtime_config, runtime_state)
        key = "sensor/temp"
        runtime_state.datastore[key] = "42"

        sent_frames: list[tuple[int, bytes]] = []

        async def fake_sender(command_id: int, payload: bytes) -> bool:
            sent_frames.append((command_id, payload))
            return True

        service.register_serial_sender(fake_sender)

        key_bytes = key.encode()
        payload = bytes([len(key_bytes)]) + key_bytes

        await service.handle_mcu_frame(
            Command.CMD_DATASTORE_GET.value,
            payload,
        )

        assert len(sent_frames) >= 2
        assert sent_frames[0][0] == Command.CMD_DATASTORE_GET_RESP.value
        assert sent_frames[0][1] == b"\x02" + b"42"
        assert sent_frames[1][0] == Status.ACK.value
        assert sent_frames[1][1] == struct.pack(rpc_protocol.UINT16_FORMAT, Command.CMD_DATASTORE_GET.value)

        queued = runtime_state.mqtt_publish_queue.get_nowait()
        expected_topic = topic_path(
            runtime_state.mqtt_topic_prefix,
            Topic.DATASTORE,
            "get",
            *key.split("/"),
        )
        assert queued.topic_name == expected_topic
        assert queued.payload == b"42"
        runtime_state.mqtt_publish_queue.task_done()

    asyncio.run(_run())


def test_datastore_get_from_mcu_unknown_key_returns_empty(
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

        key_bytes = b"missing"
        payload = bytes([len(key_bytes)]) + key_bytes

        await service.handle_mcu_frame(
            Command.CMD_DATASTORE_GET.value,
            payload,
        )

        assert len(sent_frames) >= 2
        assert sent_frames[0][0] == Command.CMD_DATASTORE_GET_RESP.value
        assert sent_frames[0][1] == b"\x00"
        assert sent_frames[1][0] == Status.ACK.value

        queued = runtime_state.mqtt_publish_queue.get_nowait()
        assert queued.payload == b""
        runtime_state.mqtt_publish_queue.task_done()

    asyncio.run(_run())


def test_datastore_put_from_mcu_updates_cache_and_mqtt(
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

        key = b"mode"
        value = b"auto"
        payload = bytes([len(key)]) + key + bytes([len(value)]) + value

        await service.handle_mcu_frame(
            Command.CMD_DATASTORE_PUT.value,
            payload,
        )

        assert runtime_state.datastore.get("mode") == "auto"
        queued = runtime_state.mqtt_publish_queue.get_nowait()
        expected_topic = topic_path(
            runtime_state.mqtt_topic_prefix,
            Topic.DATASTORE,
            "get",
            "mode",
        )
        assert queued.topic_name == expected_topic
        assert queued.payload == value
        runtime_state.mqtt_publish_queue.task_done()

        assert len(sent_frames) == 1
        assert sent_frames[0][0] == Status.ACK.value
        assert sent_frames[0][1] == struct.pack(rpc_protocol.UINT16_FORMAT, Command.CMD_DATASTORE_PUT.value)

    asyncio.run(_run())


def test_on_serial_disconnected_clears_pending(
    runtime_config: RuntimeConfig,
    runtime_state: RuntimeState,
    caplog: pytest.LogCaptureFixture,
) -> None:
    async def _run() -> None:
        service = BridgeService(runtime_config, runtime_state)

        runtime_state.pending_digital_reads.extend(
            [
                PendingPinRequest(pin=1, reply_context=None),
                PendingPinRequest(pin=2, reply_context=None),
            ]
        )
        runtime_state.pending_analog_reads.append(
            PendingPinRequest(pin=3, reply_context=None)
        )
        runtime_state.mcu_is_paused = True
        runtime_state.enqueue_console_chunk(b"keep", logging.getLogger())

        with caplog.at_level(logging.WARNING, logger="yunbridge.service"):
            await service.on_serial_disconnected()

        assert not runtime_state.pending_digital_reads
        assert not runtime_state.pending_analog_reads
        assert runtime_state.mcu_is_paused is False
        assert runtime_state.console_to_mcu_queue
        assert runtime_state.console_to_mcu_queue[0] == b"keep"
        assert runtime_state.console_queue_bytes == len(
            runtime_state.console_to_mcu_queue[0]
        )
        assert any("clearing" in record.message for record in caplog.records)
        assert runtime_state.serial_link_connected is False

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
            _make_inbound(
                topic_path(
                    runtime_state.mqtt_topic_prefix,
                    Topic.MAILBOX,
                    "read",
                ),
                b"",
            )
        )

        assert runtime_state.mqtt_publish_queue.qsize() == 2
        topic_payloads = [
            runtime_state.mqtt_publish_queue.get_nowait() for _ in range(2)
        ]
        # First message is the payload, second is the availability update.
        assert topic_payloads[0].topic_name == topic_path(
            runtime_state.mqtt_topic_prefix,
            Topic.MAILBOX,
            "incoming",
        )
        assert topic_payloads[0].payload == b""
        incoming_available_topic = mailbox_incoming_available_topic(
            runtime_state.mqtt_topic_prefix
        )
        assert topic_payloads[1].topic_name == incoming_available_topic
        for _ in topic_payloads:
            runtime_state.mqtt_publish_queue.task_done()

    asyncio.run(_run())


def test_mqtt_mailbox_write_blocked_when_topic_disabled(
    runtime_config: RuntimeConfig,
    runtime_state: RuntimeState,
) -> None:
    async def _run() -> None:
        runtime_state.topic_authorization = TopicAuthorization(mailbox_write=False)
        service = BridgeService(runtime_config, runtime_state)

        await service.handle_mqtt_message(
            _make_inbound(
                topic_path(
                    runtime_state.mqtt_topic_prefix,
                    Topic.MAILBOX,
                    "write",
                ),
                b"hello",
            )
        )

        assert not runtime_state.mailbox_queue
        queued = runtime_state.mqtt_publish_queue.get_nowait()
        payload = json.loads(queued.payload.decode())
        assert payload["topic"] == "mailbox"
        assert payload["action"] == "write"
        runtime_state.mqtt_publish_queue.task_done()

    asyncio.run(_run())


def test_mqtt_datastore_put_updates_local_cache(
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

        await service.handle_mqtt_message(
            _make_inbound(
                topic_path(
                    runtime_state.mqtt_topic_prefix,
                    Topic.DATASTORE,
                    "put",
                    "foo",
                ),
                b"baz",
            )
        )

        assert not sent_frames
        assert runtime_state.datastore.get("foo") == "baz"

        queued = runtime_state.mqtt_publish_queue.get_nowait()
        expected_topic = topic_path(
            runtime_state.mqtt_topic_prefix,
            Topic.DATASTORE,
            "get",
            "foo",
        )
        assert queued.topic_name == expected_topic
        assert queued.payload == b"baz"
        runtime_state.mqtt_publish_queue.task_done()

    asyncio.run(_run())


def test_mqtt_datastore_put_blocked_when_topic_disabled(
    runtime_config: RuntimeConfig,
    runtime_state: RuntimeState,
) -> None:
    async def _run() -> None:
        runtime_state.topic_authorization = TopicAuthorization(datastore_put=False)
        service = BridgeService(runtime_config, runtime_state)

        await service.handle_mqtt_message(
            _make_inbound(
                topic_path(
                    runtime_state.mqtt_topic_prefix,
                    Topic.DATASTORE,
                    "put",
                    "foo",
                ),
                b"baz",
            )
        )

        assert runtime_state.datastore.get("foo") is None

        queued = runtime_state.mqtt_publish_queue.get_nowait()
        expected_topic = topic_path(
            runtime_state.mqtt_topic_prefix,
            Topic.SYSTEM,
            "status",
        )
        assert queued.topic_name == expected_topic
        payload = json.loads(queued.payload.decode())
        assert payload["status"] == "forbidden"
        assert payload["topic"] == "datastore"
        assert payload["action"] == "put"
        assert ("bridge-error", "topic-action-forbidden") in (queued.user_properties)
        runtime_state.mqtt_publish_queue.task_done()

    asyncio.run(_run())


def test_mqtt_shell_run_blocked_when_topic_disabled(
    runtime_config: RuntimeConfig,
    runtime_state: RuntimeState,
) -> None:
    async def _run() -> None:
        runtime_state.topic_authorization = TopicAuthorization(shell_run=False)
        service = BridgeService(runtime_config, runtime_state)

        await service.handle_mqtt_message(
            _make_inbound(
                topic_path(
                    runtime_state.mqtt_topic_prefix,
                    Topic.SHELL,
                    "run",
                ),
                b"ls",
            )
        )

        queued = runtime_state.mqtt_publish_queue.get_nowait()
        expected_topic = topic_path(
            runtime_state.mqtt_topic_prefix,
            Topic.SYSTEM,
            "status",
        )
        assert queued.topic_name == expected_topic
        payload = json.loads(queued.payload.decode())
        assert payload["status"] == "forbidden"
        assert payload["topic"] == Topic.SHELL.value
        assert payload["action"] == "run"
        assert ("bridge-error", "topic-action-forbidden") in (queued.user_properties)
        runtime_state.mqtt_publish_queue.task_done()

    asyncio.run(_run())


def test_mqtt_bridge_handshake_topic_returns_snapshot(
    runtime_config: RuntimeConfig,
    runtime_state: RuntimeState,
) -> None:
    async def _run() -> None:
        runtime_state.handshake_attempts = 3
        runtime_state.link_is_synchronized = True
        service = BridgeService(runtime_config, runtime_state)

        await service.handle_mqtt_message(
            _make_inbound(
                topic_path(
                    runtime_state.mqtt_topic_prefix,
                    Topic.SYSTEM,
                    "bridge",
                    "handshake",
                    "get",
                ),
                b"",
            )
        )

        queued = runtime_state.mqtt_publish_queue.get_nowait()
        assert queued.topic_name == topic_path(
            runtime_state.mqtt_topic_prefix,
            Topic.SYSTEM,
            "bridge",
            "handshake",
            "value",
        )
        payload = json.loads(queued.payload.decode())
        assert payload["attempts"] == 3
        assert payload["synchronised"] is True
        assert ("bridge-snapshot", "handshake") in queued.user_properties
        runtime_state.mqtt_publish_queue.task_done()

    asyncio.run(_run())


def test_mqtt_bridge_summary_topic_returns_snapshot(
    runtime_config: RuntimeConfig,
    runtime_state: RuntimeState,
) -> None:
    async def _run() -> None:
        runtime_state.serial_link_connected = True
        runtime_state.handshake_successes = 5
        service = BridgeService(runtime_config, runtime_state)

        await service.handle_mqtt_message(
            _make_inbound(
                topic_path(
                    runtime_state.mqtt_topic_prefix,
                    Topic.SYSTEM,
                    "bridge",
                    "summary",
                    "get",
                ),
                b"",
            )
        )

        queued = runtime_state.mqtt_publish_queue.get_nowait()
        assert queued.topic_name == topic_path(
            runtime_state.mqtt_topic_prefix,
            Topic.SYSTEM,
            "bridge",
            "summary",
            "value",
        )
        payload = json.loads(queued.payload.decode())
        assert payload["serial_link"]["connected"] is True
        assert payload["handshake"]["successes"] == 5
        assert ("bridge-snapshot", "summary") in queued.user_properties
        runtime_state.mqtt_publish_queue.task_done()

    asyncio.run(_run())


def test_mqtt_datastore_put_without_key_is_ignored(
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

        await service.handle_mqtt_message(
            _make_inbound(
                topic_path(
                    runtime_state.mqtt_topic_prefix,
                    Topic.DATASTORE,
                    "put",
                ),
                b"value",
            )
        )

        assert not sent_frames
        assert runtime_state.mqtt_publish_queue.qsize() == 0

    asyncio.run(_run())


def test_mqtt_datastore_get_non_request_uses_cache(
    runtime_config: RuntimeConfig,
    runtime_state: RuntimeState,
) -> None:
    async def _run() -> None:
        runtime_state.datastore["foo"] = "cached"
        service = BridgeService(runtime_config, runtime_state)

        sent_frames: list[tuple[int, bytes]] = []

        async def fake_sender(command_id: int, payload: bytes) -> bool:
            sent_frames.append((command_id, payload))
            return True

        service.register_serial_sender(fake_sender)

        await service.handle_mqtt_message(
            _make_inbound(
                topic_path(
                    runtime_state.mqtt_topic_prefix,
                    Topic.DATASTORE,
                    "get",
                    "foo",
                ),
                b"",
            )
        )

        assert not sent_frames

        queued = runtime_state.mqtt_publish_queue.get_nowait()
        expected_topic = topic_path(
            runtime_state.mqtt_topic_prefix,
            Topic.DATASTORE,
            "get",
            "foo",
        )
        assert queued.topic_name == expected_topic
        assert queued.payload == b"cached"
        runtime_state.mqtt_publish_queue.task_done()

    asyncio.run(_run())


def test_mqtt_datastore_get_request_cache_hit_publishes_reply(
    runtime_config: RuntimeConfig,
    runtime_state: RuntimeState,
) -> None:
    async def _run() -> None:
        runtime_state.datastore["foo"] = "cached"
        service = BridgeService(runtime_config, runtime_state)

        sent_frames: list[tuple[int, bytes]] = []

        async def fake_sender(command_id: int, payload: bytes) -> bool:
            sent_frames.append((command_id, payload))
            return True

        service.register_serial_sender(fake_sender)

        await service.handle_mqtt_message(
            _make_inbound(
                topic_path(
                    runtime_state.mqtt_topic_prefix,
                    Topic.DATASTORE,
                    "get",
                    "foo",
                    "request",
                ),
                b"",
            )
        )

        assert not sent_frames
        queued = runtime_state.mqtt_publish_queue.get_nowait()
        assert queued.payload == b"cached"
        assert ("bridge-datastore-key", "foo") in queued.user_properties
        runtime_state.mqtt_publish_queue.task_done()

    asyncio.run(_run())


def test_mqtt_datastore_get_request_miss_responds_with_error(
    runtime_config: RuntimeConfig,
    runtime_state: RuntimeState,
) -> None:
    async def _run() -> None:
        service = BridgeService(runtime_config, runtime_state)

        await service.handle_mqtt_message(
            _make_inbound(
                topic_path(
                    runtime_state.mqtt_topic_prefix,
                    Topic.DATASTORE,
                    "get",
                    "foo",
                    "request",
                ),
                b"",
            )
        )

        queued = runtime_state.mqtt_publish_queue.get_nowait()
        assert queued.payload == b""
        assert ("bridge-error", "datastore-miss") in queued.user_properties
        runtime_state.mqtt_publish_queue.task_done()

    asyncio.run(_run())


def test_mqtt_datastore_get_non_request_miss_is_silent(
    runtime_config: RuntimeConfig,
    runtime_state: RuntimeState,
) -> None:
    async def _run() -> None:
        service = BridgeService(runtime_config, runtime_state)

        await service.handle_mqtt_message(
            _make_inbound(
                topic_path(
                    runtime_state.mqtt_topic_prefix,
                    Topic.DATASTORE,
                    "get",
                    "foo",
                ),
                b"",
            )
        )

        assert runtime_state.mqtt_publish_queue.qsize() == 0

    asyncio.run(_run())


def test_mqtt_file_write_blocked_when_topic_disabled(
    runtime_config: RuntimeConfig,
    runtime_state: RuntimeState,
) -> None:
    async def _run() -> None:
        runtime_state.topic_authorization = TopicAuthorization(file_write=False)
        service = BridgeService(runtime_config, runtime_state)

        await service.handle_mqtt_message(
            _make_inbound(
                topic_path(
                    runtime_state.mqtt_topic_prefix,
                    Topic.FILE,
                    "write",
                    "test.txt",
                ),
                b"payload",
            )
        )

        queued = runtime_state.mqtt_publish_queue.get_nowait()
        payload = json.loads(queued.payload.decode())
        assert payload["topic"] == "file"
        assert payload["action"] == "write"
        runtime_state.mqtt_publish_queue.task_done()

    asyncio.run(_run())


def test_mqtt_datastore_get_key_too_large_logs_warning(
    runtime_config: RuntimeConfig,
    runtime_state: RuntimeState,
    caplog: pytest.LogCaptureFixture,
) -> None:
    async def _run() -> None:
        service = BridgeService(runtime_config, runtime_state)

        sent_frames: list[tuple[int, bytes]] = []

        async def fake_sender(command_id: int, payload: bytes) -> bool:
            sent_frames.append((command_id, payload))
            return True

        service.register_serial_sender(fake_sender)

        long_key = "x" * 300

        with caplog.at_level(logging.WARNING, logger="yunbridge.datastore"):
            await service.handle_mqtt_message(
                _make_inbound(
                    (
                        topic_path(
                            runtime_state.mqtt_topic_prefix,
                            Topic.DATASTORE,
                            "get",
                            long_key,
                            "request",
                        )
                    ),
                    b"",
                )
            )

        assert not sent_frames
        assert runtime_state.mqtt_publish_queue.qsize() == 0
        assert any("too large" in record.message for record in caplog.records)

    asyncio.run(_run())


def test_enqueue_mqtt_drops_oldest_when_full(
    runtime_config: RuntimeConfig,
    runtime_state: RuntimeState,
) -> None:
    async def _run() -> None:
        runtime_state.mqtt_publish_queue = asyncio.Queue(maxsize=1)
        runtime_state.mqtt_queue_limit = 1
        service = BridgeService(runtime_config, runtime_state)

        first = QueuedPublish("br/test/one", b"1")
        second = QueuedPublish("br/test/two", b"2")

        await service.enqueue_mqtt(first)
        await service.enqueue_mqtt(second)

        assert runtime_state.mqtt_dropped_messages == 1
        assert runtime_state.mqtt_drop_counts.get("br/test/one") == 1
        assert runtime_state.mqtt_publish_queue.qsize() == 1

        queued = runtime_state.mqtt_publish_queue.get_nowait()
        assert queued.topic_name == "br/test/two"
        runtime_state.mqtt_publish_queue.task_done()

    asyncio.run(_run())


def test_run_command_respects_allow_list(
    runtime_config: RuntimeConfig,
    runtime_state: RuntimeState,
) -> None:
    async def _run() -> None:
        runtime_state.allowed_policy = AllowedCommandPolicy.from_iterable(
            ["/usr/bin/id"]
        )
        service = BridgeService(runtime_config, runtime_state)

        run_sync = getattr(service, "_run_command_sync")
        status, _, stderr, _ = await run_sync("/bin/true")

        assert status == Status.ERROR.value
        assert b"not allowed" in stderr

        runtime_state.allowed_policy = AllowedCommandPolicy.from_iterable(["*"])
        service_with_wildcard = BridgeService(runtime_config, runtime_state)

        run_command = getattr(service_with_wildcard, "_run_command_sync")
        status_ok, _, stderr_ok, _ = await run_command("/bin/true")

        assert status_ok == Status.OK.value
        assert stderr_ok == b""

    asyncio.run(_run())


def test_run_command_accepts_shell_metacharacters_as_literals(
    runtime_config: RuntimeConfig,
    runtime_state: RuntimeState,
) -> None:
    async def _run() -> None:
        runtime_state.allowed_policy = AllowedCommandPolicy.from_iterable(["*"])
        service = BridgeService(runtime_config, runtime_state)

        # Mock process component to avoid actual execution
        with patch("yunbridge.services.components.process.ProcessComponent.run_sync") as mock_run:
            mock_run.return_value = (Status.OK.value, b"hello; ls\n", b"", 0)

            run_sync = getattr(service, "_run_command_sync")
            status, stdout, _, _ = await run_sync("echo hello; ls")

            assert status == Status.OK.value
            assert b"hello; ls" in stdout

    asyncio.run(_run())


def test_process_run_async_accepts_complex_arguments(
    runtime_config: RuntimeConfig,
    runtime_state: RuntimeState,
) -> None:
    async def _run() -> None:
        runtime_state.allowed_policy = AllowedCommandPolicy.from_iterable(["*"])
        service = BridgeService(runtime_config, runtime_state)

        sent_frames: list[tuple[int, bytes]] = []

        async def fake_sender(command_id: int, payload: bytes) -> bool:
            sent_frames.append((command_id, payload))
            return True

        service.register_serial_sender(fake_sender)

        # Mock start_async to return a valid PID
        with patch("yunbridge.services.components.process.ProcessComponent.start_async") as mock_start:
            mock_start.return_value = 123

            await service.handle_mcu_frame(
                Command.CMD_PROCESS_RUN_ASYNC.value,
                b"echo hi && rm -rf /",
            )

            assert sent_frames
            status_id, status_payload = sent_frames[0]
            assert status_id == Command.CMD_PROCESS_RUN_ASYNC_RESP.value
            # Payload should be the PID (123)
            assert status_payload == struct.pack(rpc_protocol.UINT16_FORMAT, 123)

    asyncio.run(_run())


def test_pin_read_queue_limit_emits_error(
    runtime_config: RuntimeConfig,
    runtime_state: RuntimeState,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def _run() -> None:
        runtime_state.pending_pin_request_limit = 1
        service = BridgeService(runtime_config, runtime_state)

        sent_frames: list[tuple[int, bytes]] = []

        async def fake_sender(command_id: int, payload: bytes) -> bool:
            sent_frames.append((command_id, payload))
            return True

        service.register_serial_sender(fake_sender)

        async def stub_send(command_id: int, payload: bytes) -> bool:
            sent_frames.append((command_id, payload))
            return True

        monkeypatch.setattr(service._serial_flow, "send", stub_send)

        prefix = runtime_state.mqtt_topic_prefix

        await service.handle_mqtt_message(
            _make_inbound(
                topic_path(prefix, Topic.DIGITAL, "2", "read"),
                b"",
            ),
        )
        assert len(runtime_state.pending_digital_reads) == 1
        assert len(sent_frames) == 1

        await service.handle_mqtt_message(
            _make_inbound(
                topic_path(prefix, Topic.DIGITAL, "3", "read"),
                b"",
            ),
        )

        assert len(runtime_state.pending_digital_reads) == 1
        assert len(sent_frames) == 1

        user_props: list[tuple[str, str]] = []
        while not runtime_state.mqtt_publish_queue.empty():
            message = runtime_state.mqtt_publish_queue.get_nowait()
            user_props.extend(message.user_properties)
            runtime_state.mqtt_publish_queue.task_done()

        assert ("bridge-error", "pending-pin-overflow") in user_props

    asyncio.run(_run())


def test_legacy_mcu_pin_read_request_emits_not_implemented(
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
            b"\x0d",
        )

        assert sent_frames
        assert any(
            status_id == Status.NOT_IMPLEMENTED.value
            and b"pin-read-origin-mcu" in status_payload
            for status_id, status_payload in sent_frames
        )

    asyncio.run(_run())


def test_process_run_async_failure_emits_error(
    runtime_config: RuntimeConfig,
    runtime_state: RuntimeState,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def _run() -> None:
        service = BridgeService(runtime_config, runtime_state)

        sent_frames: list[tuple[int, bytes]] = []

        async def fake_sender(command_id: int, payload: bytes) -> bool:
            sent_frames.append((command_id, payload))
            return True

        service.register_serial_sender(fake_sender)

        async def failing_start_async(self: ProcessComponent, command: str) -> int:
            return rpc_protocol.UNKNOWN_COMMAND_ID

        monkeypatch.setattr(
            ProcessComponent,
            "start_async",
            failing_start_async,
        )

        await service.handle_mcu_frame(
            Command.CMD_PROCESS_RUN_ASYNC.value,
            b"/bin/false",
        )

        assert len(sent_frames) >= 2
        status_id, status_payload = sent_frames[0]
        assert status_id == Status.ERROR.value
        assert status_payload == b"process_run_async_failed"

        ack_id, ack_payload = sent_frames[1]
        assert ack_id == Status.ACK.value
        assert ack_payload == struct.pack(rpc_protocol.UINT16_FORMAT, Command.CMD_PROCESS_RUN_ASYNC.value)

        queued = runtime_state.mqtt_publish_queue.get_nowait()
        expected_topic = topic_path(
            runtime_state.mqtt_topic_prefix,
            Topic.SHELL,
            "run_async",
            "error",
        )
        assert queued.topic_name == expected_topic
        payload = json.loads(queued.payload.decode())
        assert payload["status"] == "error"
        assert payload["reason"] == "process_run_async_failed"
        runtime_state.mqtt_publish_queue.task_done()
        assert runtime_state.mqtt_publish_queue.qsize() == 0

    asyncio.run(_run())
