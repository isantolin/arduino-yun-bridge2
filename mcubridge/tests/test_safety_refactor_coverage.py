import asyncio
import collections
import pytest
from typing import Any, cast
from unittest.mock import MagicMock, patch, AsyncMock
from mcubridge.state.context import RuntimeState, create_runtime_state
from mcubridge.config.settings import RuntimeConfig
from mcubridge.services.runtime import BridgeService
from mcubridge.transport.serial import SerialTransport
from mcubridge.protocol.structures import QueuedPublish


def _replace_mailbox_queue(state: RuntimeState, replacement: Any) -> None:
    state.mailbox_queue = cast(collections.deque[bytes], replacement)


@pytest.mark.asyncio
async def test_metrics_cleanup_coverage(real_config: RuntimeConfig) -> None:
    from mcubridge.metrics import PrometheusExporter

    state = create_runtime_state(real_config)
    exporter = PrometheusExporter(state, host="127.0.0.1", port=0)

    # Mock diskcache resource with a closing failure
    mock_mq = MagicMock()
    mock_mq.cache = MagicMock()
    mock_mq.cache.close.side_effect = RuntimeError("Mock cleanup failure")

    _replace_mailbox_queue(state, mock_mq)

    # Trigger KeyError in unregister to hit the new except block
    with patch.object(getattr(exporter, "_registry"), "unregister", side_effect=KeyError()):
        with patch.object(getattr(exporter, "_server"), "serve_forever", return_value=None):
            with patch.object(getattr(exporter, "_server"), "shutdown", return_value=None):
                await exporter.run()


@pytest.mark.asyncio
async def test_context_cleanup_coverage(real_config: RuntimeConfig) -> None:
    state = create_runtime_state(real_config)

    # Mock a process that fails to terminate
    mock_proc = MagicMock()
    mock_proc.handle = MagicMock()
    mock_proc.handle.terminate.side_effect = ProcessLookupError("Mock process gone")
    state.running_processes[123] = mock_proc

    # Mock diskcache with AttributeError
    mock_mq = MagicMock()
    mock_mq.cache = MagicMock()
    _replace_mailbox_queue(state, mock_mq)

    state.cleanup()  # Hits new except blocks in context.py


@pytest.mark.asyncio
async def test_runtime_safety_coverage(real_config: RuntimeConfig) -> None:
    import sqlite3

    state = create_runtime_state(real_config)
    serial = AsyncMock(spec=SerialTransport)
    service = BridgeService(real_config, state, serial)

    # Trigger QueueEmpty in enqueue_mqtt finally block
    with patch.object(state.mqtt_publish_queue, "get_nowait", side_effect=asyncio.QueueEmpty()):
        await service.enqueue_mqtt(QueuedPublish(topic_name="test", payload=b""))

    # Mock Deque methods to throw sqlite3.Error for error branch coverage
    spool = getattr(service, "_mqtt_spool")
    with patch.object(spool, "append", side_effect=sqlite3.Error("DB error")):
        success = await getattr(service, "_spool_mqtt_message_locked")(QueuedPublish(topic_name="test", payload=b""))
        assert success is False

    with patch("asyncio.to_thread", side_effect=sqlite3.Error("DB error")):
        await getattr(service, "_flush_mqtt_spool_locked")()


@pytest.mark.asyncio
async def test_additional_coverage_boost(real_config: RuntimeConfig) -> None:
    state = create_runtime_state(real_config)
    # Trigger the logging.warning in _close_diskcache_resource via Exception
    mock_mq = MagicMock()
    mock_mq.cache = MagicMock()
    mock_mq.cache.close.side_effect = RuntimeError("Fatal cleanup error")
    _replace_mailbox_queue(state, mock_mq)
    # _close_diskcache_resource catches Exception internally — cleanup() must not raise.
    state.cleanup()
