"""Unit tests verifying Pure Telemetry Push mode without local HTTP WSGI server (SIL-2)."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock
import pytest

from mcubridge.config.settings import RuntimeConfig
import mcubridge.protocol.mcubridge_pb2 as pb
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
async def test_pure_telemetry_push_mode(test_config: RuntimeConfig, mock_bridge_state: RuntimeState) -> None:
    """Validate that BridgeService operates in Pure Telemetry Push mode without local HTTP exporter."""
    svc = BridgeService(test_config, mock_bridge_state, MagicMock())

    # Verify daemon does not maintain a local HTTP exporter
    assert not hasattr(svc, "exporter")

    from collections.abc import Awaitable, Callable

    # Verify telemetry publication works directly via cloud stream
    stream_mock = AsyncMock()
    setattr(svc, "_cloud_stream", stream_mock)

    metrics = pb.DaemonMetrics(cloud_queue_depth=0, cloud_dropped_messages=10)
    msg = pb.CloudQueuedPublish(
        topic_name="br/system/metrics",
        payload=metrics.SerializeToString(),
    )

    publish_cloud_msg: Callable[[pb.CloudQueuedPublish], Awaitable[bool]] = getattr(svc, "_publish_cloud_message")
    published = await publish_cloud_msg(msg)
    assert published is True

    stream_mock.send_message.assert_awaited_once()
    envelope: pb.CloudEnvelope = stream_mock.send_message.await_args[0][0]
    assert envelope.WhichOneof("payload") == "telemetry"
    assert envelope.telemetry.daemon_metrics_blob == metrics.SerializeToString()
