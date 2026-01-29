"""Tests for daemon metrics publisher."""

from __future__ import annotations

import asyncio
import msgspec
from unittest.mock import patch

import pytest

from mcubridge.metrics import (
    publish_bridge_snapshots,
    publish_metrics,
)
from mcubridge.mqtt.messages import QueuedPublish
from mcubridge.rpc import protocol
from mcubridge.state.context import RuntimeState


@pytest.mark.asyncio
async def test_publish_metrics_publishes_snapshot(
    runtime_state: RuntimeState,
) -> None:
    """Verify that publish_metrics enqueues payload with telemetry metadata."""

    event = asyncio.Event()
    captured: dict[str, QueuedPublish] = {}

    async def fake_enqueue(message: QueuedPublish) -> None:
        captured["message"] = message
        event.set()

    fake_snapshot = {
        "cpu": 99.0,
        "mem": {"free": 1024},
        "mqtt_spool_degraded": True,
        "mqtt_spool_failure_reason": "disk-full",
        "watchdog_enabled": True,
        "watchdog_interval": 7.5,
        "file_storage_limit_rejections": 1,
    }

    def _snapshot(self: RuntimeState) -> dict[str, object]:
        return fake_snapshot

    runtime_state.mqtt_topic_prefix = "test/prefix"

    with patch.object(
        RuntimeState, "build_metrics_snapshot", side_effect=_snapshot, autospec=True
    ):
        task = asyncio.create_task(
            publish_metrics(
                runtime_state,
                fake_enqueue,
                interval=0.01,
                min_interval=0.01,
            )
        )
        await asyncio.wait_for(event.wait(), timeout=0.5)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

    message = captured["message"]
    expected_topic = "test/prefix/system/metrics"

    assert message.topic_name == expected_topic
    assert msgspec.json.decode(message.payload) == fake_snapshot
    assert message.content_type == "application/json"
    assert ("bridge-spool", "disk-full") in message.user_properties
    assert ("bridge-files", "quota-blocked") in message.user_properties
    assert ("bridge-watchdog-enabled", "1") in message.user_properties
    assert ("bridge-watchdog-interval", "7.5") in message.user_properties


@pytest.mark.asyncio
async def test_publish_metrics_marks_unknown_spool_reason(
    runtime_state: RuntimeState,
) -> None:
    """Ensure bridge-spool user property defaults to 'unknown'."""

    event = asyncio.Event()
    captured: dict[str, QueuedPublish] = {}

    async def fake_enqueue(message: QueuedPublish) -> None:
        captured["message"] = message
        event.set()

    def _degraded_snapshot(self: RuntimeState) -> dict[str, object]:
        return {
            "mqtt_spool_degraded": True,
            "watchdog_enabled": False,
        }

    with patch.object(
        RuntimeState,
        "build_metrics_snapshot",
        side_effect=_degraded_snapshot,
        autospec=True,
    ):
        task = asyncio.create_task(
            publish_metrics(
                runtime_state,
                fake_enqueue,
                interval=0.01,
                min_interval=0.01,
            )
        )
        await asyncio.wait_for(event.wait(), timeout=0.5)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

    message = captured["message"]
    assert ("bridge-spool", "unknown") in message.user_properties
    assert any(key == "bridge-watchdog-enabled" for key, _ in message.user_properties)


@pytest.mark.asyncio
async def test_publish_bridge_snapshots_emits_summary_and_handshake(
    runtime_state: RuntimeState,
) -> None:
    event = asyncio.Event()
    messages: list[QueuedPublish] = []

    async def fake_enqueue(message: QueuedPublish) -> None:
        messages.append(message)
        if len(messages) >= 2:
            event.set()

    def _summary(self: RuntimeState) -> dict[str, object]:
        return {"snapshot": "summary"}

    def _handshake(self: RuntimeState) -> dict[str, object]:
        return {"snapshot": "handshake"}

    with patch.object(
        RuntimeState, "build_bridge_snapshot", side_effect=_summary, autospec=True
    ), patch.object(
        RuntimeState, "build_handshake_snapshot", side_effect=_handshake, autospec=True
    ):
        task = asyncio.create_task(
            publish_bridge_snapshots(
                runtime_state,
                fake_enqueue,
                summary_interval=0.01,
                handshake_interval=0.01,
                min_interval=0.01,
            )
        )
        await asyncio.wait_for(event.wait(), timeout=0.5)
        task.cancel()
        with pytest.raises((asyncio.CancelledError, BaseExceptionGroup)):
            await task

    topics = {message.topic_name for message in messages}
    assert (
        f"{protocol.MQTT_DEFAULT_TOPIC_PREFIX}/system/bridge/summary/value" in topics
    )
    assert (
        f"{protocol.MQTT_DEFAULT_TOPIC_PREFIX}/system/bridge/handshake/value" in topics
    )
    properties = [prop for message in messages for prop in message.user_properties]
    assert ("bridge-snapshot", "summary") in properties
    assert ("bridge-snapshot", "handshake") in properties


@pytest.mark.asyncio
async def test_publish_bridge_snapshots_noop_when_disabled(
    runtime_state: RuntimeState,
) -> None:
    messages: list[QueuedPublish] = []

    async def fake_enqueue(message: QueuedPublish) -> None:
        messages.append(message)

    task = asyncio.create_task(
        publish_bridge_snapshots(
            runtime_state,
            fake_enqueue,
            summary_interval=0.0,
            handshake_interval=0.0,
        )
    )
    await asyncio.sleep(0.05)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert messages == []
