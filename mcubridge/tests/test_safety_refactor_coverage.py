import collections
import pytest
from typing import Any, cast
from unittest.mock import MagicMock, patch
from mcubridge.state.context import RuntimeState, create_runtime_state
from mcubridge.config.settings import RuntimeConfig


def _replace_mailbox_queue(state: RuntimeState, replacement: Any) -> None:
    state.mailbox_queue = cast(collections.deque[bytes], replacement)


@pytest.mark.asyncio
async def test_metrics_cleanup_coverage(real_config: RuntimeConfig) -> None:
    from mcubridge.metrics import PrometheusExporter

    state = create_runtime_state(real_config)
    state.cleanup()  # Close real diskcache resources before replacing to avoid ResourceWarning

    exporter = PrometheusExporter(state, host="127.0.0.1", port=0)

    # Mock diskcache resource with a closing failure
    mock_mq = MagicMock()
    mock_mq.cache = MagicMock()
    mock_mq.cache.close.side_effect = RuntimeError("Mock cleanup failure")

    _replace_mailbox_queue(state, mock_mq)

    with patch.object(getattr(exporter, "_server"), "serve_forever", return_value=None):
        with patch.object(getattr(exporter, "_server"), "shutdown", return_value=None):
            await exporter.run()


@pytest.mark.asyncio
async def test_context_cleanup_coverage(real_config: RuntimeConfig) -> None:
    state = create_runtime_state(real_config)
    state.cleanup()  # Close real diskcache resources before replacing to avoid ResourceWarning

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
    pass
