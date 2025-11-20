"""Unit tests for BridgeService lifecycle helpers."""
from __future__ import annotations

import asyncio
import json
import logging
import struct

import pytest

from yunbridge.config.settings import RuntimeConfig
from yunbridge.services.runtime import BridgeService
from yunbridge.policy import AllowedCommandPolicy
from yunbridge.state.context import (
    PendingDatastoreRequest,
    PendingPinRequest,
    RuntimeState,
)
from yunbridge.mqtt import InboundMessage, PublishableMessage, QOSLevel
from yunbridge.rpc.protocol import Command, Status
from yunbridge.services.components.process import ProcessComponent


def _make_inbound(
    topic: str,
    payload: bytes = b"",
    *,
    qos: QOSLevel = QOSLevel.QOS_0,
    retain: bool = False,
) -> InboundMessage:
    return InboundMessage(
        topic_name=topic,
        payload=payload,
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
                await service.handle_mcu_frame(
                    Command.CMD_LINK_SYNC_RESP.value,
                    nonce,
                )
            elif command_id == Command.CMD_GET_VERSION.value:
                flow.on_frame_received(
                    Command.CMD_GET_VERSION_RESP.value,
                    b"\x01\x02",
                )
            elif command_id == Command.CMD_CONSOLE_WRITE.value:
                flow.on_frame_received(
                    Status.ACK.value,
                    struct.pack(">H", Command.CMD_CONSOLE_WRITE.value),
                )
            return True

        service.register_serial_sender(fake_sender)

        runtime_state.enqueue_console_chunk(b"hello", logging.getLogger())
        runtime_state.mcu_is_paused = False
        runtime_state.mcu_version = (1, 2)

        await service.on_serial_connected()

        assert sent_frames
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


def test_datastore_get_from_mcu_returns_cached_value(
    runtime_config: RuntimeConfig,
    runtime_state: RuntimeState,
) -> None:
    async def _run() -> None:
        service = BridgeService(runtime_config, runtime_state)
        runtime_state.datastore["sensor/temp"] = "42"

        sent_frames: list[tuple[int, bytes]] = []

        async def fake_sender(command_id: int, payload: bytes) -> bool:
            sent_frames.append((command_id, payload))
            return True

        service.register_serial_sender(fake_sender)

        key_bytes = b"sensor/temp"
        payload = bytes([len(key_bytes)]) + key_bytes

        await service.handle_mcu_frame(
            Command.CMD_DATASTORE_GET.value,
            payload,
        )

        assert len(sent_frames) >= 2
        assert sent_frames[0][0] == Command.CMD_DATASTORE_GET_RESP.value
        assert sent_frames[0][1] == b"\x02" + b"42"
        assert sent_frames[1][0] == Status.ACK.value
        assert sent_frames[1][1] == struct.pack(
            ">H", Command.CMD_DATASTORE_GET.value
        )

        queued = runtime_state.mqtt_publish_queue.get_nowait()
        assert queued.topic_name.endswith("/datastore/get/sensor/temp")
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
        assert queued.topic_name.endswith("/datastore/get/mode")
        assert queued.payload == value
        runtime_state.mqtt_publish_queue.task_done()

        assert len(sent_frames) == 1
        assert sent_frames[0][0] == Status.ACK.value
        assert sent_frames[0][1] == struct.pack(
            ">H", Command.CMD_DATASTORE_PUT.value
        )

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
        runtime_state.pending_datastore_gets.append(
            PendingDatastoreRequest(key="key", reply_context=None)
        )
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
            _make_inbound(
                f"{runtime_state.mqtt_topic_prefix}/mailbox/read",
                b"",
            )
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
                f"{runtime_state.mqtt_topic_prefix}/datastore/put/foo",
                b"baz",
            )
        )

        assert not sent_frames
        assert runtime_state.datastore.get("foo") == "baz"

        queued = runtime_state.mqtt_publish_queue.get_nowait()
        assert queued.topic_name.endswith("/datastore/get/foo")
        assert queued.payload == b"baz"
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
                f"{runtime_state.mqtt_topic_prefix}/datastore/put",
                b"value",
            )
        )

        assert not sent_frames
        assert runtime_state.mqtt_publish_queue.qsize() == 0

    asyncio.run(_run())


def test_mqtt_datastore_get_non_request_updates_cache(
    runtime_config: RuntimeConfig,
    runtime_state: RuntimeState,
) -> None:
    async def _run() -> None:
        runtime_state.datastore["foo"] = "cached"
        service = BridgeService(runtime_config, runtime_state)

        sent_frames: list[tuple[int, bytes]] = []

        async def fake_sender(command_id: int, payload: bytes) -> bool:
            sent_frames.append((command_id, payload))
            if command_id == Command.CMD_DATASTORE_GET.value:
                await service.handle_mcu_frame(
                    Command.CMD_DATASTORE_GET_RESP.value,
                    b"\x03new",
                )
            return True

        service.register_serial_sender(fake_sender)

        await service.handle_mqtt_message(
            _make_inbound(
                f"{runtime_state.mqtt_topic_prefix}/datastore/get/foo",
                b"",
            )
        )

        assert sent_frames
        payloads: list[bytes] = []
        while not runtime_state.mqtt_publish_queue.empty():
            message = runtime_state.mqtt_publish_queue.get_nowait()
            payloads.append(message.payload)
            runtime_state.mqtt_publish_queue.task_done()

        assert payloads
        assert all(payload == b"new" for payload in payloads)
        assert runtime_state.datastore["foo"] == "new"

    asyncio.run(_run())


def test_mqtt_datastore_get_send_failure_removes_pending(
    runtime_config: RuntimeConfig,
    runtime_state: RuntimeState,
) -> None:
    async def _run() -> None:
        service = BridgeService(runtime_config, runtime_state)

        sent_frames: list[tuple[int, bytes]] = []

        async def failing_sender(command_id: int, payload: bytes) -> bool:
            sent_frames.append((command_id, payload))
            return False

        service.register_serial_sender(failing_sender)

        await service.handle_mqtt_message(
            _make_inbound(
                f"{runtime_state.mqtt_topic_prefix}/datastore/get/foo/request",
                b"",
            )
        )

        assert sent_frames
        assert not runtime_state.pending_datastore_gets

        queued = runtime_state.mqtt_publish_queue.get_nowait()
        assert queued.payload == b""
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
                        f"{runtime_state.mqtt_topic_prefix}/datastore/get/"
                        f"{long_key}/request"
                    ),
                    b"",
                )
            )

        assert not sent_frames
        assert not runtime_state.pending_datastore_gets
        assert runtime_state.mqtt_publish_queue.qsize() == 0
        assert any("too large" in record.message for record in caplog.records)

    asyncio.run(_run())


def test_mqtt_datastore_get_roundtrip_updates_cache(
    runtime_config: RuntimeConfig,
    runtime_state: RuntimeState,
    caplog: pytest.LogCaptureFixture,
) -> None:
    async def _run() -> None:
        service = BridgeService(runtime_config, runtime_state)

        sent_frames: list[tuple[int, bytes]] = []
        pending_observed = False

        async def fake_sender(command_id: int, payload: bytes) -> bool:
            nonlocal pending_observed
            sent_frames.append((command_id, payload))
            if command_id == Status.ACK.value:
                return True
            if command_id == Command.CMD_DATASTORE_GET.value:
                pending_observed = (
                    bool(runtime_state.pending_datastore_gets)
                    and runtime_state.pending_datastore_gets[0].key
                    == "foo"
                )
                await service.handle_mcu_frame(
                    Command.CMD_DATASTORE_GET_RESP.value,
                    b"\x03bar",
                )
            return True

        service.register_serial_sender(fake_sender)

        with caplog.at_level(logging.WARNING, logger="yunbridge.datastore"):
            await service.handle_mqtt_message(
                _make_inbound(
                    (
                        f"{runtime_state.mqtt_topic_prefix}/datastore/get/foo"
                        "/request"
                    ),
                    b"",
                )
            )

        assert sent_frames
        assert any(
            frame_id == Command.CMD_DATASTORE_GET.value
            for frame_id, _ in sent_frames
        )
        assert pending_observed
        assert runtime_state.datastore.get("foo") == "bar"
        assert not any(
            "without pending" in record.message for record in caplog.records
        )

        queued_payloads: list[bytes] = []
        while not runtime_state.mqtt_publish_queue.empty():
            message = runtime_state.mqtt_publish_queue.get_nowait()
            queued_payloads.append(message.payload)
            runtime_state.mqtt_publish_queue.task_done()

        assert queued_payloads
        assert all(payload == b"bar" for payload in queued_payloads)

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


def test_run_command_respects_allow_list(
    runtime_config: RuntimeConfig,
    runtime_state: RuntimeState,
) -> None:
    async def _run() -> None:
        service = BridgeService(runtime_config, runtime_state)

        run_sync = getattr(service, "_run_command_sync")
        status, _, stderr, _ = await run_sync("/bin/true")

        assert status == Status.ERROR.value
        assert b"not allowed" in stderr

        runtime_state.allowed_policy = AllowedCommandPolicy.from_iterable(
            ["*"]
        )
        service_with_wildcard = BridgeService(runtime_config, runtime_state)

        run_command = getattr(service_with_wildcard, "_run_command_sync")
        status_ok, _, stderr_ok, _ = await run_command("/bin/true")

        assert status_ok == Status.OK.value
        assert stderr_ok == b""

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

        async def failing_start_async(
            self: ProcessComponent, command: str
        ) -> int:
            return 0xFFFF

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
        assert ack_payload == struct.pack(
            ">H", Command.CMD_PROCESS_RUN_ASYNC.value
        )

        queued = runtime_state.mqtt_publish_queue.get_nowait()
        assert queued.topic_name.endswith("/sh/run_async/error")
        payload = json.loads(queued.payload.decode())
        assert payload["status"] == "error"
        assert payload["reason"] == "process_run_async_failed"
        runtime_state.mqtt_publish_queue.task_done()
        assert runtime_state.mqtt_publish_queue.qsize() == 0

    asyncio.run(_run())
