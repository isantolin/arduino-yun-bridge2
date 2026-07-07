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
from mcubridge.protocol.structures import PROTOBUF_CONTENT_TYPE
from mcubridge.protocol import mcubridge_pb2 as pb
from mcubridge.protocol import protocol
from mcubridge.state.context import RuntimeState


@pytest.mark.asyncio
async def test_publish_metrics_publishes_snapshot(
    runtime_state: RuntimeState,
) -> None:
    """Verify that publish_metrics enqueues payload with telemetry metadata."""

    event = asyncio.Event()
    captured: dict[str, pb.CloudQueuedPublish] = {}

    async def fake_enqueue(message: pb.CloudQueuedPublish) -> None:
        captured["message"] = message
        event.set()

    # [SIL-2] State updated directly on context for extra_props logic
    runtime_state.cloud_spool_degraded = True
    runtime_state.cloud_spool_failure_reason = "disk-full"
    runtime_state.watchdog_enabled = True
    runtime_state.watchdog_interval = 7.5
    runtime_state.file_storage_limit_rejections = 1

    fake_snapshot = pb.DaemonMetrics(
        cloud_spool_degraded=True,
        cloud_spool_failure_reason="disk-full",
        watchdog_enabled=True,
        watchdog_interval=7.5,
    )

    runtime_state.cloud_topic_prefix = "test/prefix"

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
    decoded = pb.DaemonMetrics()
    decoded.ParseFromString(message.payload)
    assert decoded.cloud_spool_degraded
    assert decoded.cloud_spool_failure_reason == "disk-full"
    assert message.content_type == PROTOBUF_CONTENT_TYPE
    props = [(p.key, p.value) for p in message.user_properties]
    assert ("bridge-spool", "disk-full") in props
    assert ("bridge-files", "quota-blocked") in props
    assert ("bridge-watchdog-enabled", "1") in props
    assert ("bridge-watchdog-interval", "7.5") in props


@pytest.mark.asyncio
async def test_publish_metrics_marks_unknown_spool_reason(
    runtime_state: RuntimeState,
) -> None:
    """Ensure bridge-spool user property defaults to 'unknown'."""

    event = asyncio.Event()
    captured: dict[str, pb.CloudQueuedPublish] = {}

    async def fake_enqueue(message: pb.CloudQueuedPublish) -> None:
        captured["message"] = message
        event.set()

    def mock_build_metrics_degraded(self: Any) -> Any:
        return pb.DaemonMetrics(
            cloud_spool_degraded=True,
        )

    with patch.object(
        RuntimeState,
        "build_metrics_snapshot",
        side_effect=mock_build_metrics_degraded,
        autospec=True,
    ):
        runtime_state.cloud_spool_degraded = True
        runtime_state.cloud_spool_failure_reason = None
        runtime_state.watchdog_enabled = False

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
    props = [(p.key, p.value) for p in message.user_properties]
    assert ("bridge-spool", "unknown") in props
    assert any(key == "bridge-watchdog-enabled" for key, _ in props)


@pytest.mark.asyncio
async def test_publish_bridge_snapshots_emits_summary_and_handshake(
    runtime_state: RuntimeState,
) -> None:
    event = asyncio.Event()
    messages: list[pb.CloudQueuedPublish] = []

    async def fake_enqueue(message: pb.CloudQueuedPublish) -> None:
        messages.append(message)
        if len(messages) >= 2:
            event.set()

    from mcubridge.protocol import mcubridge_pb2 as pb

    def mock_build_bridge_snap(self: Any) -> Any:
        return pb.BridgeSnapshot(
            serial_link=pb.SerialLinkSnapshot(),
            handshake=pb.HandshakeSnapshot(),
            serial_pipeline=pb.SerialPipelineSnapshot(),
            serial_flow=pb.SerialFlowSnapshot(
                commands_sent=0,
                commands_acked=0,
                retries=0,
                failures=0,
                last_event_unix=0.0,
            ),
        )

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
    assert f"{protocol.CLOUD_DEFAULT_TOPIC_PREFIX}/system/bridge/summary/value" in topics
    assert f"{protocol.CLOUD_DEFAULT_TOPIC_PREFIX}/system/bridge/handshake/value" in topics
    props = [(p.key, p.value) for message in messages for p in message.user_properties]
    assert ("bridge-snapshot", "summary") in props
    assert ("bridge-snapshot", "handshake") in props


@pytest.mark.asyncio
async def test_publish_bridge_snapshots_noop_when_disabled(
    runtime_state: RuntimeState,
) -> None:
    messages: list[pb.CloudQueuedPublish] = []

    async def fake_enqueue(message: pb.CloudQueuedPublish) -> None:
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
