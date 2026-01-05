"""Unit tests for RuntimeState helpers."""

from __future__ import annotations

import asyncio
import errno
from collections.abc import Iterator
import logging
from typing import cast

import pytest

from yunbridge.config.settings import RuntimeConfig
from yunbridge.mqtt.messages import QueuedPublish
from yunbridge.mqtt.spool import MQTTPublishSpool
from yunbridge.rpc import protocol
from yunbridge.rpc.protocol import Command, Status
from yunbridge.state.context import RuntimeState, create_runtime_state


class _ListHandler(logging.Handler):
    def __init__(self) -> None:
        super().__init__()
        self.records: list[logging.LogRecord] = []

    def emit(self, record: logging.LogRecord) -> None:
        self.records.append(record)


@pytest.fixture()
def logger_spy() -> Iterator[tuple[logging.Logger, _ListHandler]]:
    logger = logging.getLogger("yunbridge.tests")
    handler = _ListHandler()
    logger.addHandler(handler)
    previous_level = logger.level
    logger.setLevel(logging.WARNING)
    try:
        yield logger, handler
    finally:
        logger.removeHandler(handler)
        logger.setLevel(previous_level)


def test_enqueue_console_chunk_trims_and_drops(
    runtime_state: RuntimeState,
    logger_spy: tuple[logging.Logger, _ListHandler],
) -> None:
    logger, handler = logger_spy

    runtime_state.enqueue_console_chunk(b"a" * 128, logger)
    assert runtime_state.console_queue_bytes == 64
    assert runtime_state.console_to_mcu_queue[-1] == b"a" * 64
    assert runtime_state.console_truncated_chunks == 1
    assert runtime_state.console_truncated_bytes == 64

    runtime_state.enqueue_console_chunk(b"b" * 64, logger)
    assert runtime_state.console_queue_bytes == 64
    assert runtime_state.console_to_mcu_queue[-1] == b"b" * 64
    assert runtime_state.console_dropped_chunks == 1
    assert runtime_state.console_dropped_bytes == 64

    warnings = [record.getMessage() for record in handler.records]
    assert any("Console chunk truncated" in message for message in warnings)
    assert any("Dropping oldest console chunk" in message for message in warnings)


def test_enqueue_mailbox_message_respects_limits(
    runtime_state: RuntimeState,
    logger_spy: tuple[logging.Logger, _ListHandler],
) -> None:
    logger, handler = logger_spy

    assert runtime_state.enqueue_mailbox_message(b"a" * 16, logger) is True
    assert runtime_state.enqueue_mailbox_message(b"b" * 16, logger) is True
    assert runtime_state.mailbox_queue_bytes == 32

    # Next message should trigger eviction and be accepted after trimming
    assert runtime_state.enqueue_mailbox_message(b"c" * 40, logger) is True
    assert runtime_state.mailbox_queue_bytes == 32
    assert len(runtime_state.mailbox_queue) == 1
    assert runtime_state.mailbox_queue[-1] == b"c" * 32
    assert runtime_state.mailbox_truncated_messages == 1
    assert runtime_state.mailbox_truncated_bytes == 8
    assert runtime_state.mailbox_dropped_messages == 2
    assert runtime_state.mailbox_dropped_bytes == 32

    warnings = [record.getMessage() for record in handler.records]
    assert any("Mailbox message truncated" in message for message in warnings)
    assert any("Dropping oldest mailbox message" in message for message in warnings)


def test_enqueue_mailbox_incoming_respects_limits(
    runtime_state: RuntimeState,
    logger_spy: tuple[logging.Logger, _ListHandler],
) -> None:
    logger, handler = logger_spy

    assert runtime_state.enqueue_mailbox_incoming(b"x" * 16, logger) is True
    assert runtime_state.enqueue_mailbox_incoming(b"y" * 16, logger) is True
    assert runtime_state.mailbox_incoming_queue_bytes == 32

    assert runtime_state.enqueue_mailbox_incoming(b"z" * 40, logger) is True
    assert runtime_state.mailbox_incoming_queue_bytes == 32
    assert len(runtime_state.mailbox_incoming_queue) == 1
    assert runtime_state.mailbox_incoming_queue[-1] == b"z" * 32
    assert runtime_state.mailbox_incoming_truncated_messages == 1
    assert runtime_state.mailbox_incoming_truncated_bytes == 8
    assert runtime_state.mailbox_incoming_dropped_messages == 2
    assert runtime_state.mailbox_incoming_dropped_bytes == 32

    warnings = [record.getMessage() for record in handler.records]
    assert any("Mailbox incoming message truncated" in message for message in warnings)
    assert any(
        "Dropping oldest mailbox incoming message" in message for message in warnings
    )


def test_requeue_console_chunk_front_restores_bytes(
    runtime_state: RuntimeState,
) -> None:
    runtime_state.enqueue_console_chunk(b"hello", logging.getLogger())
    queued = runtime_state.pop_console_chunk()
    assert runtime_state.console_queue_bytes == 0

    runtime_state.requeue_console_chunk_front(queued)

    assert runtime_state.console_queue_bytes == len(queued)
    assert runtime_state.console_to_mcu_queue[0] == queued


def test_mqtt_queue_respects_config(
    runtime_state: RuntimeState,
    runtime_config: RuntimeConfig,
) -> None:
    assert runtime_state.mqtt_publish_queue.maxsize == runtime_config.mqtt_queue_limit
    assert runtime_state.mqtt_queue_limit == runtime_config.mqtt_queue_limit


def test_watchdog_tracking(runtime_state: RuntimeState) -> None:
    assert runtime_state.watchdog_beats == 0
    runtime_state.record_watchdog_beat(123.0)
    assert runtime_state.watchdog_beats == 1
    assert runtime_state.last_watchdog_beat == 123.0


def test_metrics_snapshot_exposes_error_counters(
    runtime_state: RuntimeState,
) -> None:
    runtime_state.record_serial_flow_event("sent")
    runtime_state.record_serial_decode_error()
    runtime_state.record_serial_crc_error()
    runtime_state.record_mcu_status(Status.CRC_MISMATCH)
    runtime_state.record_mcu_status(Status.CRC_MISMATCH)
    runtime_state.record_mqtt_drop("bridge/status")
    runtime_state.mqtt_spool_degraded = True
    runtime_state.mqtt_spool_failure_reason = "disk-full"
    runtime_state.mqtt_spool_retry_attempts = 2
    runtime_state.mqtt_spool_backoff_until = 123.0
    runtime_state.mqtt_spool_last_error = "disk-full"
    runtime_state.mqtt_spool_recoveries = 1

    snapshot = runtime_state.build_metrics_snapshot()

    assert snapshot["serial"]["commands_sent"] == 1
    assert snapshot["serial_decode_errors"] == 1
    assert snapshot["serial_crc_errors"] == 1
    assert snapshot["mcu_status"]["CRC_MISMATCH"] == 2
    assert snapshot["mqtt_drop_counts"]["bridge/status"] == 1
    assert snapshot["mqtt_spool_degraded"] is True
    assert snapshot["mqtt_spool_failure_reason"] == "disk-full"
    assert snapshot["mqtt_spool_retry_attempts"] == 2
    assert snapshot["mqtt_spool_backoff_until"] == 123.0
    assert snapshot["mqtt_spool_last_error"] == "disk-full"
    assert snapshot["mqtt_spool_recoveries"] == 1


def test_metrics_snapshot_includes_spool_snapshot(
    runtime_state: RuntimeState,
) -> None:
    class _StubSpool:
        def snapshot(self) -> dict[str, int]:
            return {"pending": 5, "limit": 128}

    runtime_state.mqtt_spool = cast(MQTTPublishSpool, _StubSpool())

    snapshot = runtime_state.build_metrics_snapshot()

    assert snapshot["spool_pending"] == 5
    assert snapshot["spool_limit"] == 128


def test_handshake_snapshot_reflects_state(
    runtime_state: RuntimeState,
) -> None:
    runtime_state.link_is_synchronized = True
    runtime_state.handshake_attempts = 4
    runtime_state.handshake_failures = 1
    runtime_state.link_nonce_length = 16

    snapshot = runtime_state.build_handshake_snapshot()

    assert snapshot["synchronised"] is True
    assert snapshot["attempts"] == 4
    assert snapshot["failures"] == 1
    assert snapshot["nonce_length"] == 16


def test_serial_pipeline_snapshot_tracks_events(
    runtime_state: RuntimeState,
) -> None:
    runtime_state.record_serial_pipeline_event(
        {
            "event": "start",
            "command_id": Command.CMD_DIGITAL_WRITE.value,
            "attempt": 1,
            "timestamp": 10.0,
        }
    )
    runtime_state.record_serial_pipeline_event(
        {
            "event": "ack",
            "command_id": Command.CMD_DIGITAL_WRITE.value,
            "attempt": 1,
            "timestamp": 10.1,
            "ack_received": True,
        }
    )
    runtime_state.record_serial_pipeline_event(
        {
            "event": "success",
            "command_id": Command.CMD_DIGITAL_WRITE.value,
            "attempt": 1,
            "timestamp": 10.2,
            "ack_received": True,
            "status": Status.OK.value,
        }
    )

    snapshot = runtime_state.build_serial_pipeline_snapshot()
    assert snapshot["inflight"] is None
    last = snapshot["last_completion"]
    assert last is not None
    assert last["event"] == "success"
    assert last["status_name"] == "OK"
    assert last["duration"] > 0


def test_bridge_snapshot_combines_sections(
    runtime_state: RuntimeState,
) -> None:
    runtime_state.serial_link_connected = True
    runtime_state.handshake_attempts = 2
    runtime_state.mcu_version = (1, 2)
    runtime_state.record_serial_pipeline_event(
        {
            "event": "start",
            "command_id": Command.CMD_DIGITAL_READ.value,
            "attempt": 1,
            "timestamp": 20.0,
        }
    )
    runtime_state.record_serial_pipeline_event(
        {
            "event": "failure",
            "command_id": Command.CMD_DIGITAL_READ.value,
            "attempt": 1,
            "timestamp": 20.5,
            "status": Status.TIMEOUT.value,
        }
    )

    bridge = runtime_state.build_bridge_snapshot()
    assert bridge["serial_link"]["connected"] is True
    assert bridge["handshake"]["attempts"] == 2
    assert bridge["mcu_version"] == {"major": 1, "minor": 2}
    last = bridge["serial_pipeline"]["last_completion"]
    assert last is not None and last["event"] == "failure"


def test_create_runtime_state_marks_spool_degraded(
    runtime_config: RuntimeConfig,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class _BoomSpool:
        def __init__(self, *_args, **_kwargs) -> None:
            raise RuntimeError("boom")

    monkeypatch.setattr(
        "yunbridge.state.context.MQTTPublishSpool",
        _BoomSpool,
    )

    state = create_runtime_state(runtime_config)

    assert state.mqtt_spool is None
    assert state.mqtt_spool_degraded is True
    assert state.mqtt_spool_failure_reason == "initialization_failed"
    assert state.mqtt_spool_last_error
    assert "boom" in state.mqtt_spool_last_error


def test_stash_mqtt_message_disables_spool_on_failure(
    runtime_config: RuntimeConfig,
) -> None:
    async def _run() -> None:
        state = create_runtime_state(runtime_config)
        if state.mqtt_spool is not None:
            state.mqtt_spool.close()

        class _BrokenSpool:
            def append(self, _message: QueuedPublish) -> None:
                raise RuntimeError("disk-full")

            def close(self) -> None:
                return None

        state.mqtt_spool = cast(MQTTPublishSpool, _BrokenSpool())
        message = QueuedPublish(
            topic_name=f"{protocol.MQTT_DEFAULT_TOPIC_PREFIX}/test",
            payload=b"{}",
        )
        stored = await state.stash_mqtt_message(message)

        assert stored is False
        assert state.mqtt_spool is None
        assert state.mqtt_spool_degraded is True
        assert state.mqtt_spool_errors == 1
        assert state.mqtt_dropped_messages == 0
        assert state.mqtt_spool_failure_reason == "append_failed"
        assert state.mqtt_spool_last_error is not None
        assert "append_failed" in state.mqtt_spool_last_error

    asyncio.run(_run())


def test_flush_mqtt_spool_handles_pop_failure(
    runtime_config: RuntimeConfig,
) -> None:
    async def _run() -> None:
        state = create_runtime_state(runtime_config)
        if state.mqtt_spool is not None:
            state.mqtt_spool.close()

        class _FailingSpool:
            def pop_next(self) -> QueuedPublish:
                raise RuntimeError("read-error")

            def requeue(self, _message: QueuedPublish) -> None:
                return None

            def close(self) -> None:
                return None

        state.mqtt_spool = cast(MQTTPublishSpool, _FailingSpool())
        await state.flush_mqtt_spool()

        assert state.mqtt_spool is None
        assert state.mqtt_spool_degraded is True
        assert state.mqtt_spool_errors == 1
        assert state.mqtt_spool_failure_reason == "pop_failed"
        assert state.mqtt_spool_last_error is not None
        assert "pop_failed" in state.mqtt_spool_last_error

    asyncio.run(_run())


def test_spool_fallback_updates_state(
    runtime_config: RuntimeConfig,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def _run() -> None:
        state = create_runtime_state(runtime_config)
        spool = state.mqtt_spool
        assert spool is not None

        queue = getattr(spool, "_disk_queue")

        def _boom(_record: object) -> None:
            raise OSError(errno.ENOSPC, "disk full")

        monkeypatch.setattr(queue, "append", _boom)

        stored = await state.stash_mqtt_message(
            QueuedPublish(
                topic_name=f"{protocol.MQTT_DEFAULT_TOPIC_PREFIX}/test",
                payload=b"{}",
            )
        )

        assert stored is True
        assert state.mqtt_spool is not None
        assert state.mqtt_spool_degraded is True
        assert state.mqtt_spool_failure_reason == "disk_full"
        assert state.mqtt_spool_last_error == "disk_full"
        assert state.mqtt_spool_errors >= 1

    asyncio.run(_run())


def test_ensure_spool_recovers_after_disable(
    runtime_config: RuntimeConfig,
    tmp_path_factory: pytest.TempPathFactory,
) -> None:
    async def _run() -> None:
        runtime_config.mqtt_spool_dir = str(tmp_path_factory.mktemp("spool"))
        state = create_runtime_state(runtime_config)
        assert state.mqtt_spool is not None
        state.mqtt_spool.close()
        state.mqtt_spool = None
        state.mqtt_spool_degraded = True
        state.mqtt_spool_failure_reason = "test"

        recovered = await state.ensure_spool()

        assert recovered is True
        assert state.mqtt_spool is not None
        assert state.mqtt_spool_degraded is False
        assert state.mqtt_spool_failure_reason is None
        assert state.mqtt_spool_recoveries == 1

    asyncio.run(_run())
