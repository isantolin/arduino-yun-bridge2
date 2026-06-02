import asyncio
import pytest
from unittest.mock import MagicMock, patch, AsyncMock
from mcubridge.state.context import create_runtime_state
from mcubridge.config.settings import RuntimeConfig
from mcubridge.services.runtime import BridgeService
from mcubridge.transport.serial import SerialTransport
from mcubridge.protocol.structures import QueuedPublish


@pytest.mark.asyncio
async def test_metrics_cleanup_coverage(real_config: RuntimeConfig) -> None:
    from mcubridge.metrics import PrometheusExporter
    state = create_runtime_state(real_config)
    exporter = PrometheusExporter(state, host="127.0.0.1", port=0)
    mock_mq = MagicMock()
    mock_mq.cache = MagicMock()
    mock_mq.cache._local = MagicMock()
    mock_mq.cache._local.con = MagicMock()
    setattr(mock_mq.cache, "_local", mock_mq.cache._local)
    mock_mq.cache._local.con.close.side_effect = RuntimeError("Mock cleanup failure")
    state.mailbox_queue = mock_mq
    with patch.object(exporter._registry, "unregister", side_effect=KeyError()):
        exporter._server = MagicMock()
        exporter._collector = MagicMock()
        await exporter.run()


@pytest.mark.asyncio
async def test_context_cleanup_coverage(real_config: RuntimeConfig) -> None:
    state = create_runtime_state(real_config)
    mock_proc = MagicMock()
    mock_proc.handle = MagicMock()
    mock_proc.handle.terminate.side_effect = ProcessLookupError("Mock process gone")
    state.running_processes[123] = mock_proc
    mock_mq = MagicMock()
    mock_mq.cache = MagicMock()
    if hasattr(mock_mq.cache, "_local"):
        del mock_mq.cache._local
    state.mailbox_queue = mock_mq
    state.cleanup()


@pytest.mark.asyncio
async def test_runtime_safety_coverage(real_config: RuntimeConfig) -> None:
    state = create_runtime_state(real_config)
    serial = AsyncMock(spec=SerialTransport)
    service = BridgeService(real_config, state, serial)
    with patch.object(state.mqtt_publish_queue, "get_nowait", side_effect=asyncio.QueueEmpty()):
        await service.enqueue_mqtt(QueuedPublish(topic_name="test", payload=b""))
    from pathlib import Path
    mock_path = MagicMock(spec=Path)
    with patch.object(service, "_list_mqtt_spool_files", return_value=[mock_path]):
        with patch("asyncio.to_thread", side_effect=[[mock_path], FileNotFoundError()]):
            await service._trim_mqtt_spool_locked()

@pytest.mark.asyncio
async def test_service_minimal_coverage_boost(real_config: RuntimeConfig) -> None:
    state = create_runtime_state(real_config)
    serial = AsyncMock(spec=SerialTransport)
    service = BridgeService(real_config, state, serial)
    with patch("mcubridge.protocol.protocol.build_link_reset_ack", return_value=b"ack"):
        await service._apply_link_reset(MagicMock())
    with patch("pathlib.Path.iterdir", side_effect=FileNotFoundError()):
        assert service._list_mqtt_spool_files() == []
