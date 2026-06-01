import asyncio
import pytest
from unittest.mock import MagicMock, patch, AsyncMock
from mcubridge.state.context import create_runtime_state
from mcubridge.services.runtime import BridgeService
from mcubridge.transport.serial import SerialTransport
from mcubridge.protocol.structures import QueuedPublish


@pytest.mark.asyncio
async def test_metrics_cleanup_coverage(real_config):
    from mcubridge.metrics import PrometheusExporter

    state = create_runtime_state(real_config)
    exporter = PrometheusExporter(state, host="127.0.0.1", port=0)

    # Mock diskcache resource with a closing failure
    mock_mq = MagicMock()
    mock_mq.cache = MagicMock()
    mock_mq.cache._local = MagicMock()
    mock_mq.cache._local.con = MagicMock()
    mock_mq.cache._local.con.close.side_effect = RuntimeError("Mock cleanup failure")

    state.mailbox_queue = mock_mq

    # Trigger KeyError in unregister to hit the new except block
    with patch.object(exporter._registry, "unregister", side_effect=KeyError()):
        # Mock server and collector to enter the block
        exporter._server = MagicMock()
        exporter._collector = MagicMock()
        await exporter.run()


@pytest.mark.asyncio
async def test_context_cleanup_coverage(real_config):
    state = create_runtime_state(real_config)

    # Mock a process that fails to terminate
    mock_proc = MagicMock()
    mock_proc.handle = MagicMock()
    mock_proc.handle.terminate.side_effect = ProcessLookupError("Mock process gone")
    state.running_processes[123] = mock_proc

    # Mock diskcache with AttributeError
    mock_mq = MagicMock()
    mock_mq.cache = MagicMock()
    # No _local to trigger AttributeError in _close_diskcache_resource
    del mock_mq.cache._local
    state.mailbox_queue = mock_mq

    state.cleanup()  # Hits new except blocks in context.py


@pytest.mark.asyncio
async def test_runtime_safety_coverage(real_config):
    state = create_runtime_state(real_config)
    serial = AsyncMock(spec=SerialTransport)
    service = BridgeService(real_config, state, serial)

    # Trigger QueueEmpty in enqueue_mqtt finally block
    with patch.object(state.mqtt_publish_queue, "get_nowait", side_effect=asyncio.QueueEmpty()):
        await service.enqueue_mqtt(QueuedPublish(topic_name="test", payload=b""))

    # Trigger FileNotFoundError in spool unlink
    from pathlib import Path

    mock_path = MagicMock(spec=Path)
    # We need to reach the point where unlink is called.
    # _trim_mqtt_spool_locked calls _list_mqtt_spool_files, then iterates.
    with patch.object(service, "_list_mqtt_spool_files", return_value=[mock_path]):
        with patch("asyncio.to_thread", side_effect=[[mock_path], FileNotFoundError()]):
            await service._trim_mqtt_spool_locked()
