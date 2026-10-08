"""Unit tests verifying Pure Telemetry Push mode without local HTTP WSGI server (SIL-2)."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import mcubridge.protocol.mcubridge_pb2 as pb
import pytest
from mcubridge.config.settings import RuntimeConfig
from mcubridge.services.runtime import BridgeService
from mcubridge.state.context import RuntimeState, create_runtime_state


def _make_config() -> RuntimeConfig:
    return RuntimeConfig(
        allowed_commands=("echo", "ls"),
        serial_shared_secret=b"testsharedsecret",
        allow_non_tmp_paths=True,
    )


@pytest.fixture
def test_config() -> RuntimeConfig:
    return _make_config()


@pytest.fixture
def mock_bridge_state(test_config: RuntimeConfig) -> RuntimeState:
    return create_runtime_state(test_config)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("topic_name", "telemetry_field"),
    [
        ("br/system/metrics", "daemon_metrics_blob"),
        ("br/system/bridge/summary/value", "bridge_snapshot_blob"),
        ("br/system/bridge/handshake/value", "handshake_snapshot_blob"),
        ("br/system/status", "system_status_blob"),
        ("br/other/topic", "system_status_blob"),
    ],
)
async def test_pure_telemetry_push_mode(
    topic_name: str,
    telemetry_field: str,
    test_config: RuntimeConfig,
    mock_bridge_state: RuntimeState,
) -> None:
    """Validate that BridgeService operates in Pure Telemetry Push mode without local HTTP exporter."""
    svc = BridgeService(test_config, mock_bridge_state, MagicMock())

    # Verify daemon does not maintain a local HTTP exporter
    assert not hasattr(svc, "exporter")

    from collections.abc import Awaitable, Callable

    # Verify telemetry publication works directly via cloud stream
    stream_mock = AsyncMock()
    svc.cloud_stream = stream_mock

    metrics = pb.DaemonMetrics(cloud_queue_depth=0, cloud_dropped_messages=10)
    payload = metrics.SerializeToString() if telemetry_field == "daemon_metrics_blob" else b"telemetry-payload"
    msg = pb.CloudQueuedPublish(
        topic_name=topic_name,
        payload=payload,
    )

    publish_cloud_msg: Callable[[pb.CloudQueuedPublish], Awaitable[bool]] = svc.publish_cloud_message
    published = await publish_cloud_msg(msg)
    assert published is True

    stream_mock.send_message.assert_awaited_once()
    envelope: pb.CloudEnvelope = stream_mock.send_message.await_args[0][0]
    assert envelope.WhichOneof("payload") == "telemetry"
    assert getattr(envelope.telemetry, telemetry_field) == payload
    assert {field.name for field, _ in envelope.telemetry.ListFields()} == {telemetry_field}
