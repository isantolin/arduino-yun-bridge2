"""Tests for daemon metrics publisher."""

from __future__ import annotations

import asyncio
from typing import Any
from unittest.mock import patch

import pytest
from mcubridge.metrics import (
    publish_bridge_snapshots,
    publish_metrics,
)
from mcubridge.protocol.structures import PROTOBUF_CONTENT_TYPE, QueuedPublish
from mcubridge.protocol import mcubridge_pb2 as pb
from mcubridge.protocol import protocol
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

    fake_snapshot = pb.DaemonMetrics()
    fake_snapshot.mqtt_spool_degraded = True
    fake_snapshot.mqtt_spool_failure_reason = "disk-full"
    fake_snapshot.file_storage_limit_rejections = 1

    runtime_state.mqtt_topic_prefix = "test/prefix"
    runtime_state.watchdog_interval = 7.5

    def mock_build_metrics(self: Any) -> Any:
        return fake_snapshot

    with patch.object(
        RuntimeState,
        "build_metrics_snapshot",
        side_effect=mock_build_metrics,
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
    expected_topic = "test/prefix/system/metrics"

    assert message.topic_name == expected_topic
    assert pb.DaemonMetrics.FromString(message.payload) == fake_snapshot
    assert message.content_type == PROTOBUF_CONTENT_TYPE
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

    def mock_build_metrics_degraded(self: Any) -> Any:
        snap = pb.DaemonMetrics()
        snap.mqtt_spool_degraded = True
        return snap

    with patch.object(
        RuntimeState,
        "build_metrics_snapshot",
        side_effect=mock_build_metrics_degraded,
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

    def mock_build_bridge_snap(self: Any) -> Any:
        return pb.BridgeSnapshot()

    def mock_build_handshake_snap(self: Any) -> Any:
        return pb.HandshakeSnapshot()

    with (
        patch.object(
            RuntimeState,
            "build_bridge_snapshot",
            side_effect=mock_build_bridge_snap,
            autospec=True,
        ),
        patch.object(
            RuntimeState,
            "build_handshake_snapshot",
            side_effect=mock_build_handshake_snap,
            autospec=True,
        ),
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
    assert f"{protocol.MQTT_DEFAULT_TOPIC_PREFIX}/system/bridge/summary/value" in topics
    assert f"{protocol.MQTT_DEFAULT_TOPIC_PREFIX}/system/bridge/handshake/value" in topics
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
