"""Unit tests verifying Pure Telemetry Push mode without local HTTP WSGI server (SIL-2)."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

from mcubridge.protocol import cloud_gateway_pb2, mcu_wire_pb2
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


@pytest.mark.parametrize(
    ("topic_name", "telemetry_field"),
    [
        ("br/evt/SystemService/Metrics", "daemon_metrics_blob"),
        ("br/evt/SystemService/BridgeSnapshot", "bridge_snapshot_blob"),
        ("br/evt/SystemService/HandshakeSnapshot", "handshake_snapshot_blob"),
        ("br/evt/SystemService/StatusReport", "system_status_blob"),
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

    # Verify telemetry publication works directly via cloud RPC client
    svc._cloud_rpc_client = MagicMock()
    mock_writer = MagicMock()
    mock_writer.drain = AsyncMock()
    svc._cloud_writer = mock_writer
    svc.send_cloud_telemetry = AsyncMock(return_value=True)

    metrics = mcu_wire_pb2.DaemonMetrics(cloud_queue_depth=0, cloud_dropped_messages=10)
    payload = metrics.SerializeToString() if telemetry_field == "daemon_metrics_blob" else b"telemetry-payload"
    msg = cloud_gateway_pb2.CloudQueuedPublish(
        topic_name=topic_name,
        payload=payload,
    )

    publish_cloud_msg: Callable[[cloud_gateway_pb2.CloudQueuedPublish], Awaitable[bool]] = svc.publish_cloud_message
    published = await publish_cloud_msg(msg)
    assert published is True

    svc.send_cloud_telemetry.assert_awaited_once()
    kwargs = svc.send_cloud_telemetry.await_args.kwargs
    assert kwargs.get(telemetry_field) == payload
