"""Tests for MQTT publish spool durability and fallback."""

from __future__ import annotations

import pytest
pytestmark = pytest.mark.skip(reason="Obsolete API")

import logging
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
from mcubridge.protocol.structures import QueuedPublish
from mcubridge.mqtt.spool import MQTTPublishSpool
from mcubridge.protocol import protocol


def _make_message(
    topic: str,
    payload: str = "hello",
    *,
    user_properties: list[tuple[str, str]] | None = None,
) -> QueuedPublish:
    return QueuedPublish(
        topic_name=topic,
        payload=payload.encode(),
        qos=0,
        retain=False,
        user_properties=user_properties or [],
    )


def test_spool_roundtrip(tmp_path: Path) -> None:
    # Emulate /tmp path for persistence
    spool_dir = tmp_path / "tmp" / "spool"
    spool_dir.mkdir(parents=True)

    spool = MQTTPublishSpool(spool_dir.as_posix(), limit=4)
    message = _make_message(
        f"{protocol.MQTT_DEFAULT_TOPIC_PREFIX}/system/test",
        user_properties=[("k", "v")],
    )

    spool.append(message)

    assert spool.pending == 1

    restored = spool.pop_next()
    assert restored is not None
    assert restored.topic_name == message.topic_name
    assert restored.payload == message.payload
    assert restored.user_properties == message.user_properties
    assert spool.pending == 0


def test_spool_trim_limit(tmp_path: Path) -> None:
    spool_dir = tmp_path / "tmp" / "spool"
    spool_dir.mkdir(parents=True)

    spool = MQTTPublishSpool(spool_dir.as_posix(), limit=2)
    for idx in range(5):
        spool.append(_make_message(f"topic/{idx}", str(idx)))

    # zict.LRU should maintain exactly 'limit' items
    assert spool.pending == 2
    snapshot = spool.snapshot()
    assert snapshot["dropped_due_to_limit"] == 3
    assert snapshot["trim_events"] >= 1


def test_spool_snapshot_reports_pending(tmp_path: Path) -> None:
    spool_dir = tmp_path / "tmp" / "spool"
    spool_dir.mkdir(parents=True)

    spool = MQTTPublishSpool(spool_dir.as_posix(), limit=3)
    spool.append(_make_message("topic/1"))
    spool.append(_make_message("topic/2"))

    snapshot = spool.snapshot()

    assert snapshot["pending"] == 2
    assert snapshot["limit"] == 3
    assert snapshot["corrupt_dropped"] == 0


def test_spool_skips_corrupt_rows(tmp_path: Path, caplog: pytest.LogCaptureFixture) -> None:
    spool_dir = tmp_path / "tmp" / "spool"
    spool_dir.mkdir(parents=True)

    spool = MQTTPublishSpool(spool_dir.as_posix(), limit=4)
    spool.append(_make_message("topic/first"))

    # We simulate corruption by mocking the LRU cache to fail on a specific key
    original_spool = spool._spool
    mock_lru = MagicMock(spec=dict)
    # Forward keys() and len() to original to maintain state
    mock_lru.keys.side_effect = original_spool.keys
    mock_lru.__len__.side_effect = original_spool.__len__

    # Mock pop to fail for key "1"
    def mock_pop(key, default=None):
        if key == "1":
            raise ValueError("Corrupt msgpack")
        return original_spool.pop(key, default)

    mock_lru.pop.side_effect = mock_pop

    with patch.object(spool, "_spool", mock_lru):
        spool.append(_make_message("topic/first")) # key "0"
        spool._tail = 2 # Force key "1" existence

        caplog.set_level(logging.WARNING, "mcubridge.mqtt.spool")

        restored_one = spool.pop_next() # key "0" (success)
        restored_two = spool.pop_next() # key "1" (failure)

        assert restored_one is not None
        assert restored_one.topic_name == "topic/first"
        assert restored_two is None
        assert spool.snapshot()["corrupt_dropped"] == 1


def test_spool_fallback_on_init_failure(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Spool degrades if directory creation fails."""

    # Force Path.mkdir to fail
    monkeypatch.setattr(Path, "mkdir", MagicMock(side_effect=PermissionError("No access")))

    # Path must look like /tmp to attempt disk init
    spool = MQTTPublishSpool("/tmp/protected", limit=5)

    assert spool.is_degraded
    # Should still work in memory (via empty dict fallback)
    spool.append(_make_message("topic/fallback"))
    assert spool.pending == 1
    popped = spool.pop_next()
    assert popped is not None
    assert popped.topic_name == "topic/fallback"


def test_spool_requeue_success(tmp_path: Path) -> None:
    spool_dir = tmp_path / "tmp" / "spool"
    spool_dir.mkdir(parents=True)

    spool = MQTTPublishSpool(spool_dir.as_posix(), limit=4)
    message = _make_message("topic/requeue")
    spool.append(message)

    popped = spool.pop_next()
    assert popped is not None
    assert popped.topic_name == "topic/requeue"

    spool.requeue(popped)
    assert spool.pending == 1

    popped_again = spool.pop_next()
    assert popped_again is not None
    assert popped_again.topic_name == "topic/requeue"


def test_spool_pop_skips_gaps(tmp_path: Path) -> None:
    """Test that pop_next skips missing keys gracefully."""
    spool_dir = tmp_path / "tmp" / "spool"
    spool_dir.mkdir(parents=True)

    spool = MQTTPublishSpool(spool_dir.as_posix(), limit=10)
    spool.append(_make_message("topic/1"))
    spool._tail = 5 # Create a gap between 0 and 5
    spool.append(_make_message("topic/2"))

    msg1 = spool.pop_next()
    assert msg1 is not None
    assert msg1.topic_name == "topic/1"

    msg2 = spool.pop_next()
    assert msg2 is not None
    assert msg2.topic_name == "topic/2"
    assert spool._head == 6
