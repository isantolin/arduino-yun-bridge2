"""Tests for daemon metrics publisher."""

from __future__ import annotations

import asyncio
from typing import Any
from unittest.mock import AsyncMock

import pytest
from mcubridge.metrics import (
    publish_bridge_snapshots,
    publish_metrics,
)
from mcubridge.protocol import mcubridge_pb2 as pb
from mcubridge.protocol import protocol
from mcubridge.protocol.structures import PROTOBUF_CONTENT_TYPE
from mcubridge.state.context import RuntimeState
from pytest_mock import MockerFixture


@pytest.mark.asyncio
async def test_publish_metrics_publishes_snapshot(runtime_state: RuntimeState, mocker: MockerFixture) -> None:
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

    mocker.patch.object(
        RuntimeState,
        "build_metrics_snapshot",
        side_effect=mock_build_metrics,
        autospec=True,
    )
    task = asyncio.create_task(
        publish_metrics(
            runtime_state,
            fake_enqueue,
            interval=0.01,
            min_interval=0.01,
        )
    )
    async with asyncio.timeout(0.5):
        await event.wait()
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
async def test_publish_metrics_marks_unknown_spool_reason(runtime_state: RuntimeState, mocker: MockerFixture) -> None:
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

    mocker.patch.object(
        RuntimeState,
        "build_metrics_snapshot",
        side_effect=mock_build_metrics_degraded,
        autospec=True,
    )
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
    async with asyncio.timeout(0.5):
        await event.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    message = captured["message"]
    props = [(p.key, p.value) for p in message.user_properties]
    assert ("bridge-spool", "unknown") in props
    assert any(key == "bridge-watchdog-enabled" for key, _ in props)


@pytest.mark.asyncio
async def test_publish_bridge_snapshots_emits_summary_and_handshake(
    runtime_state: RuntimeState, mocker: MockerFixture
) -> None:
    event = asyncio.Event()
    messages: list[pb.CloudQueuedPublish] = []

    async def fake_enqueue(message: pb.CloudQueuedPublish) -> None:
        messages.append(message)
        if len(messages) >= 2:
            event.set()

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

    mocker.patch.object(
        RuntimeState,
        "build_bridge_snapshot",
        side_effect=mock_build_bridge_snap,
        autospec=True,
    )
    mocker.patch.object(
        RuntimeState,
        "build_handshake_snapshot",
        side_effect=mock_build_handshake_snap,
        autospec=True,
    )
    task = asyncio.create_task(
        publish_bridge_snapshots(
            runtime_state,
            fake_enqueue,
            summary_interval=0.01,
            handshake_interval=0.01,
            min_interval=0.01,
        )
    )
    async with asyncio.timeout(0.5):
        await event.wait()
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


@pytest.mark.asyncio
async def test_emit_bridge_snapshot_error_paths(runtime_state: RuntimeState, mocker: MockerFixture) -> None:
    import mcubridge.metrics

    emit_fn = getattr(mcubridge.metrics, "_emit_bridge_snapshot")
    mock_log = mocker.patch("mcubridge.metrics.logger.error")
    mock_critical = mocker.patch("mcubridge.metrics.logger.critical")

    async def _failing_enqueue(_: pb.CloudQueuedPublish) -> None:
        raise OSError("Disk full")

    # OSError in enqueue is caught and logged as error
    await emit_fn(runtime_state, _failing_enqueue, flavor="summary")
    assert mock_log.call_count == 1

    # AttributeError in builder is caught and logged as critical
    mocker.patch(
        "mcubridge.metrics._build_bridge_snapshot_message",
        side_effect=AttributeError("Corrupted snapshot"),
    )
    await emit_fn(runtime_state, _failing_enqueue, flavor="summary")
    assert mock_log.call_count == 1
    assert mock_critical.call_count == 1


@pytest.mark.asyncio
async def test_publish_metrics_oserror_recovery(runtime_state: RuntimeState) -> None:
    calls = 0

    async def _failing_enqueue(_: pb.CloudQueuedPublish) -> None:
        nonlocal calls
        calls += 1
        raise OSError("Transient IO failure")

    task = asyncio.create_task(
        publish_metrics(
            runtime_state,
            _failing_enqueue,
            interval=0.01,
            min_interval=0.01,
        )
    )
    await asyncio.sleep(0.05)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert calls >= 1


@pytest.mark.asyncio
async def test_publish_bridge_snapshots_loop_error_recovery(runtime_state: RuntimeState, mocker: MockerFixture) -> None:
    calls = 0

    async def _failing_enqueue(_: pb.CloudQueuedPublish) -> None:
        pass

    async def _failing_emit(*_: object, **__: object) -> None:
        nonlocal calls
        calls += 1
        raise OSError("Loop IO failure")

    mocker.patch("mcubridge.metrics._emit_bridge_snapshot", side_effect=_failing_emit)
    task = asyncio.create_task(
        publish_bridge_snapshots(
            runtime_state,
            _failing_enqueue,
            summary_interval=0.01,
            handshake_interval=0.01,
            min_interval=0.01,
        )
    )
    await asyncio.sleep(0.05)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert calls >= 1


@pytest.mark.asyncio
async def test_emit_bridge_snapshot_attribute_error(runtime_state: RuntimeState, mocker: MockerFixture) -> None:
    from collections.abc import Awaitable, Callable
    import mcubridge.metrics as metrics_mod

    enqueue = AsyncMock()
    mocker.patch.object(runtime_state, "build_bridge_snapshot", side_effect=AttributeError("Missing attr"))
    emit_snapshot: Callable[..., Awaitable[None]] = getattr(metrics_mod, "_emit_bridge_snapshot")
    await emit_snapshot(runtime_state, enqueue, flavor="summary")
    assert enqueue.call_count == 0
