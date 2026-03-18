"""Tests for the periodic status writer."""

from __future__ import annotations

import asyncio
import logging
from types import SimpleNamespace
from typing import Any, cast

import msgspec
import pytest
from mcubridge.mqtt.spool import MQTTPublishSpool
from mcubridge.policy import AllowedCommandPolicy
from mcubridge.protocol import protocol
from mcubridge.state import status
from mcubridge.state.context import RuntimeState, SupervisorStats


def test_status_writer_publishes_metrics(monkeypatch, tmp_path):
    async def run() -> None:
        status_path = tmp_path / "status.json"
        writes: list[dict[str, object]] = []

        def fake_write(payload: Any) -> None:
            data = msgspec.json.encode(payload)
            writes.append(msgspec.json.decode(data))
            status_path.write_bytes(data)

        monkeypatch.setattr(status, "STATUS_FILE", status_path)
        monkeypatch.setattr(
            status,
            "_write_status_file",
            fake_write,
        )

        state = RuntimeState()
        state.mqtt_queue_limit = 42
        # Use record methods for read-only properties
        for _ in range(3):
            state.record_mqtt_drop(f"{protocol.MQTT_DEFAULT_TOPIC_PREFIX}/test")

        state.datastore["foo"] = "bar"
        state.enqueue_mailbox_message(b"abc", logging.getLogger())
        state.mailbox_queue_bytes = 3
        state.mailbox_dropped_messages = 1
        state.mailbox_truncated_messages = 1
        state.mailbox_truncated_bytes = 2
        state.mailbox_dropped_bytes = 3
        state.enqueue_mailbox_incoming(b"xyz", logging.getLogger())
        state.mailbox_incoming_queue_bytes = 3
        state.mailbox_incoming_dropped_messages = 1
        state.mailbox_incoming_truncated_messages = 2
        state.mailbox_incoming_truncated_bytes = 5
        state.mailbox_incoming_dropped_bytes = 4
        state.console_to_mcu_queue.append(b"1")
        state.console_queue_bytes = 1
        state.console_dropped_chunks = 2
        state.console_dropped_bytes = 8
        state.console_truncated_chunks = 1
        state.console_truncated_bytes = 4
        state.mcu_is_paused = True
        state.mark_transport_connected()
        state.mark_synchronized()
        state.mark_transport_connected()
        state.record_handshake_attempt()
        state.record_handshake_attempt()
        state.allowed_policy = AllowedCommandPolicy.from_iterable(["ls"])
        state.mcu_version = (2, 5)
        state.file_system_root = "/tmp/bridge"
        state.file_storage_bytes_used = 2048
        state.file_storage_quota_bytes = 4096
        state.file_write_max_bytes = 512
        state.file_write_limit_rejections = 1
        state.file_storage_limit_rejections = 2
        state.supervisor_stats = {
            "file": SupervisorStats(restarts=3),
        }
        # Spooled metrics are also read-only properties
        for _ in range(10):
            state.record_mqtt_spool()
        state.mqtt_spooled_replayed = 4
        for _ in range(2):
            state.record_mqtt_spool_error()

        state.mqtt_spool_degraded = True
        state.mqtt_spool_failure_reason = "disk-full"
        state.mqtt_spool_retry_attempts = 3
        state.mqtt_spool_backoff_until = 42.0
        state.mqtt_spool_last_error = "append_failed"
        state.mqtt_spool_recoveries = 1
        state.mqtt_spool = cast(
            MQTTPublishSpool,
            SimpleNamespace(pending=7, limit=32),
        )
        state.watchdog_enabled = True
        state.watchdog_interval = 7.5
        for _ in range(11):
            state.record_watchdog_beat(101.0)

        task = asyncio.create_task(status.status_writer(state, 0))
        for _ in range(10):
            if writes:
                break
            await asyncio.sleep(0.01)

        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

        assert writes, "status_writer no generó payload"
        payload = writes[0]

        assert payload["mqtt_queue_limit"] == 42
        assert payload["mqtt_messages_dropped"] >= 3
        assert payload["mqtt_drop_counts"] == {f"{protocol.MQTT_DEFAULT_TOPIC_PREFIX}/test": 3}
        assert payload["datastore_keys"] == ["foo"]
        assert payload["mailbox_size"] == 1
        assert payload["mailbox_bytes"] == 3
        assert payload["mailbox_dropped_messages"] == 1
        assert payload["mailbox_truncated_messages"] == 1
        assert payload["mailbox_truncated_bytes"] == 2
        assert payload["mailbox_dropped_bytes"] == 3
        assert payload["mailbox_incoming_dropped_messages"] == 1
        assert payload["mailbox_incoming_truncated_messages"] == 2
        assert payload["mailbox_incoming_truncated_bytes"] == 5
        assert payload["mailbox_incoming_dropped_bytes"] == 4
        assert payload["mcu_paused"] is True
        assert payload["console_queue_size"] == 1
        assert payload["console_queue_bytes"] == 1
        assert payload["console_dropped_chunks"] == 2
        assert payload["console_dropped_bytes"] == 8
        assert payload["console_truncated_chunks"] == 1
        assert payload["console_truncated_bytes"] == 4
        assert payload["allowed_commands"] == ["ls"]
        assert payload["link_synchronised"] is False
        assert payload["mcu_version"] == {"major": 2, "minor": 5}
        assert payload["file_storage_root"] == "/tmp/bridge"
        assert payload["file_storage_bytes_used"] == 2048
        assert payload["file_storage_quota_bytes"] == 4096
        assert payload["file_write_max_bytes"] == 512
        assert payload["file_write_limit_rejections"] == 1
        assert payload["file_storage_limit_rejections"] == 2
        assert "bridge" in payload
        bridge_snapshot = payload["bridge"]
        handshake_snapshot = bridge_snapshot["handshake"]
        assert handshake_snapshot["attempts"] >= 2
        assert payload["mqtt_spooled_messages"] >= 10
        assert payload["mqtt_spooled_replayed"] == 4
        assert payload["mqtt_spool_errors"] >= 2
        assert payload["mqtt_spool_degraded"] is True
        assert payload["mqtt_spool_failure_reason"] == "disk-full"
        assert payload["mqtt_spool_retry_attempts"] == 3
        assert payload["mqtt_spool_backoff_until"] == 42.0
        assert payload["mqtt_spool_last_error"] == "append_failed"
        assert payload["mqtt_spool_recoveries"] == 1
        assert payload["mqtt_spool_pending"] == 7
        assert payload["watchdog_enabled"] is True
        assert payload["watchdog_interval"] == 7.5
        assert payload["watchdog_beats"] >= 11
        assert payload["watchdog_last_beat"] == 101.0

        assert status_path.exists()
        file_payload = msgspec.json.decode(status_path.read_bytes())
        assert file_payload["mqtt_queue_limit"] == 42

    asyncio.run(run())
