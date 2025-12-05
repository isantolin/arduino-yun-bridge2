"""Tests for daemon metrics publisher."""
from __future__ import annotations

import asyncio
import json

import pytest

from yunbridge.metrics import publish_metrics
from yunbridge.mqtt import PublishableMessage
from yunbridge.state.context import RuntimeState


@pytest.mark.asyncio
async def test_publish_metrics_publishes_snapshot(
    runtime_state: RuntimeState,
) -> None:
    """Verify that publish_metrics enqueues payload with telemetry metadata."""

    event = asyncio.Event()
    captured: dict[str, PublishableMessage] = {}

    async def fake_enqueue(message: PublishableMessage) -> None:
        captured["message"] = message
        event.set()

    fake_snapshot = {
        "cpu": 99.0,
        "mem": {"free": 1024},
        "mqtt_spool_degraded": True,
        "mqtt_spool_failure_reason": "disk-full",
        "watchdog_enabled": True,
        "watchdog_interval": 7.5,
    }

    def _snapshot() -> dict[str, object]:
        return fake_snapshot

    runtime_state.build_metrics_snapshot = (  # type: ignore[assignment]
        _snapshot
    )
    runtime_state.mqtt_topic_prefix = "test/prefix"

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
    expected_payload = json.dumps(fake_snapshot).encode("utf-8")

    assert message.topic_name == expected_topic
    assert message.payload == expected_payload
    assert message.content_type == "application/json"
    assert ("bridge-spool", "disk-full") in message.user_properties
    assert ("bridge-watchdog-enabled", "1") in message.user_properties
    assert ("bridge-watchdog-interval", "7.5") in message.user_properties


@pytest.mark.asyncio
async def test_publish_metrics_marks_unknown_spool_reason(
    runtime_state: RuntimeState,
) -> None:
    """Ensure bridge-spool user property defaults to 'unknown'."""

    event = asyncio.Event()
    captured: dict[str, PublishableMessage] = {}

    async def fake_enqueue(message: PublishableMessage) -> None:
        captured["message"] = message
        event.set()

    def _degraded_snapshot() -> dict[str, object]:
        return {
            "mqtt_spool_degraded": True,
            "watchdog_enabled": False,
        }

    runtime_state.build_metrics_snapshot = (  # type: ignore[assignment]
        _degraded_snapshot
    )
    runtime_state.mqtt_topic_prefix = "br"

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
    assert any(
        key == "bridge-watchdog-enabled" for key, _ in message.user_properties
    )
