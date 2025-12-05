"""Tests for MQTT publish spool durability."""
from __future__ import annotations

import logging
from pathlib import Path

import pytest

from yunbridge.mqtt import PublishableMessage, QOSLevel
from yunbridge.mqtt.spool import MQTTPublishSpool


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


def test_spool_snapshot_reports_pending(tmp_path: Path) -> None:
    spool = MQTTPublishSpool(tmp_path.as_posix(), limit=3)
    spool.append(_make_message("topic/1"))
    spool.append(_make_message("topic/2"))

    snapshot = spool.snapshot()

    assert snapshot["pending"] == 2
    assert snapshot["limit"] == 3


def test_spool_skips_corrupt_rows(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    spool = MQTTPublishSpool(tmp_path.as_posix(), limit=4)
    spool.append(_make_message("topic/first"))

    # Inject a corrupt entry directly into the underlying durable queue.
    spool._queue.put(b"not-a-dict")  # type: ignore[attr-defined]
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
