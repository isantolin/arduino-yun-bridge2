from typing import Any, cast
import asyncio
from types import SimpleNamespace

import msgspec
import pytest
from mcubridge.state import status
from mcubridge.state.context import RuntimeState, SupervisorStats
from mcubridge.protocol import protocol
from mcubridge.policy import AllowedCommandPolicy
from mcubridge.mqtt.spool import MQTTPublishSpool


def test_status_writer_publishes_metrics(monkeypatch: Any, tmp_path: Any):
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
        try:
            state.mqtt_queue_limit = 42
            # Direct attribute updates for read-only properties (formerly record methods)
            topic = f"{protocol.MQTT_DEFAULT_TOPIC_PREFIX}/test"
            state.mqtt_drop_counts[topic] = 3
            state.mqtt_dropped_messages = 3
            state.metrics.mqtt_messages_dropped.inc(3)

            state.datastore["foo"] = "bar"
            state.mailbox_queue.append(b"abc")
            state.mailbox_queue_bytes = 3
            state.mailbox_dropped_messages = 1
            state.mailbox_truncated_messages = 1
            state.mailbox_truncated_bytes = 2
            state.mailbox_dropped_bytes = 3
            state.mailbox_incoming_queue.append(b"xyz")
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

            state.handshake_attempts = 2
            state.metrics.handshake_attempts.inc(2)

            state.allowed_policy = AllowedCommandPolicy.from_iterable(["ls"])
            state.mcu_version = (2, 5, 0)
            state.file_system_root = ".tmp_tests/bridge"
            state.file_storage_bytes_used = 2048
            state.file_storage_quota_bytes = 4096
            state.file_write_max_bytes = 512
            state.file_write_limit_rejections = 1
            state.file_storage_limit_rejections = 2
            state.supervisor_stats = {
                "file": SupervisorStats(restarts=3),
            }

            state.mqtt_spooled_messages = 10
            state.metrics.mqtt_spooled_messages.inc(10)
            state.mqtt_spooled_replayed = 4
            state.mqtt_spool_errors = 2
            state.metrics.mqtt_spool_errors.inc(2)

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
            state.watchdog_beats = 11
            state.metrics.watchdog_beats.inc(11)
            state.last_watchdog_beat = 101.0

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
            assert isinstance(payload["mqtt_messages_dropped"], int)
            assert payload["mqtt_messages_dropped"] >= 3
            assert payload["mqtt_drop_counts"] == {topic: 3}
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
            assert payload["mcu_version"] == {"major": 2, "minor": 5, "patch": 0}
            assert payload["file_storage_root"] == ".tmp_tests/bridge"
            assert payload["file_storage_bytes_used"] == 2048
            assert payload["file_storage_quota_bytes"] == 4096
            assert payload["file_write_max_bytes"] == 512
            assert payload["file_write_limit_rejections"] == 1
            assert payload["file_storage_limit_rejections"] == 2
            assert "bridge" in payload
            bridge_snapshot = payload["bridge"]
            handshake_snapshot = bridge_snapshot["handshake"]  # type: ignore[reportUnknownVariableType]
            assert handshake_snapshot["attempts"] >= 2
            assert isinstance(payload["mqtt_spooled_messages"], int)
            assert payload["mqtt_spooled_messages"] >= 10
            assert payload["mqtt_spooled_replayed"] == 4
            assert isinstance(payload["mqtt_spool_errors"], int)
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
            assert isinstance(payload["watchdog_beats"], int)
            assert payload["watchdog_beats"] >= 11
            assert payload["watchdog_last_beat"] == 101.0

            assert status_path.exists()
            file_payload = msgspec.json.decode(status_path.read_bytes())
            assert file_payload["mqtt_queue_limit"] == 42
        finally:
            state.cleanup()

    asyncio.run(run())
