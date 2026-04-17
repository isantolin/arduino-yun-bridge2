"""Tests for MQTT publish spool durability and fallback."""

from __future__ import annotations

import logging
from pathlib import Path
from unittest.mock import patch

import pytest

from mcubridge.protocol import protocol
from mcubridge.protocol.structures import QueuedPublish
from mcubridge.mqtt.spool import MQTTPublishSpool


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
    spool.close()


def test_spool_trim_limit(tmp_path: Path) -> None:
    spool_dir = tmp_path / "tmp" / "spool"
    spool_dir.mkdir(parents=True)

    spool = MQTTPublishSpool(spool_dir.as_posix(), limit=2)
    for idx in range(5):
        spool.append(_make_message(f"topic/{idx}", str(idx)))

    assert spool.pending == 2
    snapshot = spool.snapshot()
    assert snapshot["dropped_due_to_limit"] == 3
    spool.close()


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
    spool.close()


def test_spool_skips_corrupt_rows(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    spool_dir = tmp_path / "tmp" / "spool"
    spool_dir.mkdir(parents=True)

    spool = MQTTPublishSpool(spool_dir.as_posix(), limit=4)
    first = _make_message("topic/first")
    second = _make_message("topic/second")
    spool.append(first)
    spool.append(second)

    import msgspec
    from typing import Any

    # Capture original to avoid infinite recursion
    original_decode = msgspec.json.decode
    call_count = 0

    def _decode_side_effect(record: bytes, **kwargs: Any) -> Any:
        nonlocal call_count
        call_count += 1
        if call_count == 2:
            raise msgspec.MsgspecError("Corrupt JSON")
        return original_decode(record, **kwargs)

    caplog.set_level(logging.WARNING, "mcubridge.mqtt.spool")

    with patch("msgspec.json.decode", side_effect=_decode_side_effect):
        restored_one = spool.pop_next()
        restored_two = spool.pop_next()

    assert restored_one is not None
    assert restored_one.topic_name == "topic/first"
    assert restored_two is None
    assert spool.snapshot()["corrupt_dropped"] == 1
    spool.close()


def test_spool_fallback_on_init_failure(monkeypatch: pytest.MonkeyPatch) -> None:
    def _fail_mkdir(self: Path, parents: bool = False, exist_ok: bool = False) -> None:
        del self, parents, exist_ok
        raise PermissionError("No access")

    monkeypatch.setattr(Path, "mkdir", _fail_mkdir)

    spool = MQTTPublishSpool("/tmp/protected", limit=5)

    assert spool.is_degraded
    spool.append(_make_message("topic/fallback"))
    assert spool.pending == 1
    popped = spool.pop_next()
    assert popped is not None
    assert popped.topic_name == "topic/fallback"
    spool.close()


def test_spool_requeue_success(tmp_path: Path) -> None:
    spool_dir = tmp_path / "tmp" / "spool"
    spool_dir.mkdir(parents=True)

    spool = MQTTPublishSpool(spool_dir.as_posix(), limit=4)
    try:
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
    finally:
        spool.close()


def test_spool_persists_across_reopen(tmp_path: Path) -> None:
    spool_dir = tmp_path / "tmp" / "spool"
    spool_dir.mkdir(parents=True)

    spool = MQTTPublishSpool(spool_dir.as_posix(), limit=10)
    spool.append(_make_message("topic/1"))
    spool.append(_make_message("topic/2"))
    spool.close()  # Close before reopening to ensure flush

    reopened = MQTTPublishSpool(spool_dir.as_posix(), limit=10)
    try:
        msg1 = reopened.pop_next()
        msg2 = reopened.pop_next()

        assert msg1 is not None
        assert msg2 is not None
        assert msg1.topic_name == "topic/1"
        assert msg2.topic_name == "topic/2"
    finally:
        reopened.close()
