"""Tests for MQTT publish spool durability and fallback."""

from __future__ import annotations

import errno
import logging
from pathlib import Path
from typing import Any

import pytest

from mcubridge.mqtt.messages import QueuedPublish
from mcubridge.mqtt.spool import MQTTPublishSpool
from mcubridge.rpc import protocol


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
        f"{protocol.MQTT_DEFAULT_TOPIC_PREFIX}/system/test",
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
    # Append 5 messages. Limit is 2. 3 should be dropped.
    for idx in range(5):
        spool.append(_make_message(f"topic/{idx}", str(idx)))

    # Pending should be 2 (limit)
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

    queue: Any = getattr(spool, "_disk_queue")
    if queue is None:
        pytest.skip("Durable spool backend not available")

    # Inject a corrupt entry directly into the filesystem.
    # We use a timestamp that falls between "first" and "second" ideally,
    # or just rely on sort order. FileDeque sorts by name.
    # Existing files are like "timestamp.msg".
    # We create a file that sorts after the first one.

    # Wait a tiny bit to ensure timestamp diff or force name
    import time
    time.sleep(0.001)

    corrupt_file = tmp_path / f"{time.time_ns()}_corrupt.msg"
    corrupt_file.write_bytes(b"not-valid-msgpack-data")

    time.sleep(0.001)
    spool.append(_make_message("topic/second"))

    caplog.set_level(logging.WARNING, "mcubridge.mqtt.spool")

    # Pop first (valid)
    restored_one = spool.pop_next()
    assert restored_one is not None
    assert restored_one.topic_name == "topic/first"

    # Pop next should encounter corrupt file, log warning, delete it, and return second message
    restored_two = spool.pop_next()

    assert restored_two is not None
    assert restored_two.topic_name == "topic/second"
    assert spool.pop_next() is None

    # Check log for warning
    assert "corrupt/unreadable spool file" in caplog.text


def test_spool_fallback_on_disk_full(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Test automatic fallback to memory queue when disk write fails."""
    spool = MQTTPublishSpool(tmp_path.as_posix(), limit=5)

    queue: Any = getattr(spool, "_disk_queue")
    if queue is None:
        pytest.skip("Durable spool backend not available")

    def _boom(_record: object) -> None:
        raise OSError(errno.ENOSPC, "disk full")

    monkeypatch.setattr(queue, "append", _boom)

    # First append should fail on disk and trigger fallback
    spool.append(_make_message("topic/disk"))

    assert spool.is_degraded
    assert "disk_full" in caplog.text
    assert "Switching to memory-only mode" in caplog.text

    # Second append goes to memory
    spool.append(_make_message("topic/memory"))

    # When fallback is active, pending counts memory queue + disk queue items (if readable)
    # Here disk queue append failed, so item went to memory.
    # Total pending: 2
    assert spool.pending == 2

    # Verify pops work from memory
    # Note: pop_next drains disk first if possible, then memory.
    # Since disk append failed, disk might be empty or have partial data?
    # Actually FileDeque write uses atomic rename, so disk should be clean.
    msg1 = spool.pop_next()
    msg2 = spool.pop_next()

    assert msg1 is not None
    assert msg2 is not None

    # Order depends on whether disk queue had anything. It was empty before failure.
    # So both are in memory.
    # Order preserved in memory queue? append() adds to memory queue on failure.

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
    if queue is None:
        pytest.skip("Durable spool backend not available")

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

    # We need to patch pathlib.Path.mkdir used in FileDeque init
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


def test_spool_requeue_success(tmp_path: Path) -> None:
    spool = MQTTPublishSpool(tmp_path.as_posix(), limit=4)
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


def test_spool_requeue_fallback(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    spool = MQTTPublishSpool(tmp_path.as_posix(), limit=4)
    message = _make_message("topic/requeue_fail")

    # Mock disk queue appendleft to fail
    queue: Any = getattr(spool, "_disk_queue")
    if queue is None:
        pytest.skip("Durable spool backend not available")

    def _boom(_record: object) -> None:
        raise OSError(errno.EIO, "disk error")

    monkeypatch.setattr(queue, "appendleft", _boom)

    spool.requeue(message)

    assert spool.is_degraded
    assert spool.pending == 1

    # Should be in memory now
    popped = spool.pop_next()
    assert popped is not None
    assert popped.topic_name == "topic/requeue_fail"


def test_spool_pop_fallback(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    spool = MQTTPublishSpool(tmp_path.as_posix(), limit=4)
    spool.append(_make_message("topic/pop_fail"))

    # Mock disk queue popleft to fail
    queue: Any = getattr(spool, "_disk_queue")
    if queue is None:
        pytest.skip("Durable spool backend not available")

    def _boom() -> None:
        raise OSError(errno.EIO, "disk error")

    monkeypatch.setattr(queue, "popleft", _boom)

    # pop_next should trigger fallback and return None (since memory is empty initially)
    # The item on disk is inaccessible.
    assert spool.pop_next() is None
    assert spool.is_degraded


def test_spool_trim_fallback(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    spool = MQTTPublishSpool(tmp_path.as_posix(), limit=2)
    spool.append(_make_message("topic/1"))
    spool.append(_make_message("topic/2"))

    # Mock disk queue popleft (used in trim) to fail
    queue: Any = getattr(spool, "_disk_queue")
    if queue is None:
        pytest.skip("Durable spool backend not available")

    def _boom() -> None:
        raise OSError(errno.EIO, "disk error")

    monkeypatch.setattr(queue, "popleft", _boom)

    # Append 3rd item, triggering trim
    spool.append(_make_message("topic/3"))

    assert spool.is_degraded
