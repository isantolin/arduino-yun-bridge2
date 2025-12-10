"""Tests for MQTT publish spool durability and fallback."""
from __future__ import annotations

import errno
import logging
from pathlib import Path
from typing import Any

import pytest

from yunbridge.mqtt.messages import QueuedPublish
from yunbridge.mqtt.spool import MQTTPublishSpool


def _make_message(
    topic: str,
    payload: str = "hello",
    *,
    user_properties: tuple[tuple[str, str], ...] = (),
) -> QueuedPublish:
    return QueuedPublish(
        topic_name=topic,
        payload=payload.encode(),
        qos=0,
        retain=False,
        user_properties=user_properties,
    )


def test_spool_roundtrip(tmp_path: Path) -> None:
    spool = MQTTPublishSpool(tmp_path.as_posix(), limit=4)
    message = _make_message(
        "br/system/test",
        user_properties=(("k", "v"),),
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
    spool = MQTTPublishSpool(tmp_path.as_posix(), limit=2)
    for idx in range(5):
        spool.append(_make_message(f"topic/{idx}", str(idx)))
    assert spool.pending == 2
    snapshot = spool.snapshot()
    assert snapshot["dropped_due_to_limit"] == 3
    assert snapshot["trim_events"] >= 1


def test_spool_snapshot_reports_pending(tmp_path: Path) -> None:
    spool = MQTTPublishSpool(tmp_path.as_posix(), limit=3)
    spool.append(_make_message("topic/1"))
    spool.append(_make_message("topic/2"))

    snapshot = spool.snapshot()

    assert snapshot["pending"] == 2
    assert snapshot["limit"] == 3
    assert snapshot["corrupt_dropped"] == 0


def test_spool_skips_corrupt_rows(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    spool = MQTTPublishSpool(tmp_path.as_posix(), limit=4)
    spool.append(_make_message("topic/first"))

    # Inject a corrupt entry directly into the underlying durable queue.
    queue: Any = getattr(spool, "_disk_queue")
    queue.append(b"not-a-dict")
    spool.append(_make_message("topic/second"))

    caplog.set_level(logging.WARNING, "yunbridge.mqtt.spool")

    restored_one = spool.pop_next()
    restored_two = spool.pop_next()

    assert restored_one is not None
    assert restored_one.topic_name == "topic/first"
    assert restored_two is not None
    assert restored_two.topic_name == "topic/second"
    assert spool.pop_next() is None
    assert "Dropping corrupt MQTT spool entry" in caplog.text
    assert spool.snapshot()["corrupt_dropped"] == 1


def test_spool_fallback_on_disk_full(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Test automatic fallback to memory queue when disk write fails."""
    spool = MQTTPublishSpool(tmp_path.as_posix(), limit=5)

    def _boom(_record: object) -> None:
        raise OSError(errno.ENOSPC, "disk full")

    queue: Any = getattr(spool, "_disk_queue")
    monkeypatch.setattr(queue, "append", _boom)

    # First append should fail on disk and trigger fallback
    spool.append(_make_message("topic/disk"))

    assert spool.is_degraded
    assert "disk_full" in caplog.text
    assert "Switching to memory-only mode" in caplog.text

    # Second append goes to memory
    spool.append(_make_message("topic/memory"))

    assert spool.pending == 2

    # Verify pops work from memory
    msg1 = spool.pop_next()
    msg2 = spool.pop_next()

    assert msg1 is not None
    assert msg2 is not None
    assert msg1.topic_name == "topic/disk"
    assert msg2.topic_name == "topic/memory"


def test_spool_fallback_invokes_hook(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    reasons: list[str] = []

    spool = MQTTPublishSpool(
        tmp_path.as_posix(),
        limit=2,
        on_fallback=reasons.append,
    )

    queue: Any = getattr(spool, "_disk_queue")

    def _boom(_record: object) -> None:
        raise OSError(errno.ENOSPC, "disk full")

    monkeypatch.setattr(queue, "append", _boom)

    spool.append(_make_message("topic/hook"))

    assert reasons
    assert reasons[-1] == "disk_full"
    assert spool.is_degraded


def test_spool_fallback_on_init_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Spool degrades if directory creation fails."""

    # Force Path.mkdir to fail
    def _fail_mkdir(*args, **kwargs):
        raise PermissionError("No access")

    monkeypatch.setattr(Path, "mkdir", _fail_mkdir)

    spool = MQTTPublishSpool("/root/protected", limit=5)

    assert spool.is_degraded
    assert spool._disk_queue is None

    # Should still work in memory
    spool.append(_make_message("topic/fallback"))
    assert spool.pending == 1
    popped = spool.pop_next()
    assert popped is not None
    assert popped.topic_name == "topic/fallback"
