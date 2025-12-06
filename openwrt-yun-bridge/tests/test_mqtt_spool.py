"""Tests for MQTT publish spool durability."""
from __future__ import annotations

import errno
import logging
from pathlib import Path
from typing import Any

import pytest

from yunbridge.mqtt import PublishableMessage, QOSLevel
from yunbridge.mqtt.spool import MQTTPublishSpool, MQTTSpoolError


def _make_message(topic: str, payload: str = "hello") -> PublishableMessage:
    return PublishableMessage(
        topic_name=topic,
        payload=payload.encode(),
        qos=QOSLevel.QOS_0,
        retain=False,
    )


def test_spool_roundtrip(tmp_path: Path) -> None:
    spool = MQTTPublishSpool(tmp_path.as_posix(), limit=4)
    message = _make_message("br/system/test").with_user_property("k", "v")

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
    queue: Any = getattr(spool, "_queue")
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


def test_spool_detects_disk_full(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    spool = MQTTPublishSpool(tmp_path.as_posix(), limit=1)

    def _boom(_record: object) -> None:
        raise OSError(errno.ENOSPC, "disk full")

    queue: Any = getattr(spool, "_queue")
    monkeypatch.setattr(queue, "append", _boom)

    with pytest.raises(MQTTSpoolError) as excinfo:
        spool.append(_make_message("topic/disk"))

    assert excinfo.value.reason == "disk_full"


def test_spool_detects_generic_append_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    spool = MQTTPublishSpool(tmp_path.as_posix(), limit=1)

    def _boom(_record: object) -> None:
        raise RuntimeError("boom")

    queue: Any = getattr(spool, "_queue")
    monkeypatch.setattr(queue, "append", _boom)

    with pytest.raises(MQTTSpoolError) as excinfo:
        spool.append(_make_message("topic/boom"))

    assert excinfo.value.reason == "append_failed"
