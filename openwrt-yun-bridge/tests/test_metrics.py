"""Tests for daemon metrics publisher."""
from __future__ import annotations

import asyncio
import json
from unittest.mock import AsyncMock, MagicMock

import pytest

from yunbridge.metrics import publish_metrics
from yunbridge.state.context import RuntimeState


@pytest.mark.asyncio
async def test_publish_metrics_publishes_snapshot(
    monkeypatch: pytest.MonkeyPatch,
    runtime_state: RuntimeState,
) -> None:
    """Verify that publish_metrics enqueues a valid metrics payload."""
    mock_enqueue = AsyncMock()
    fake_snapshot = {"cpu": 99.0, "mem": {"free": 1024}}

    def _build_snapshot() -> dict:
        return fake_snapshot

    runtime_state.build_metrics_snapshot = _build_snapshot
    runtime_state.mqtt_topic_prefix = "test/prefix"

    # Run the publisher for one cycle
    try:
        await asyncio.wait_for(
            publish_metrics(runtime_state, mock_enqueue, interval=0.01, min_interval=0.01),
            timeout=0.1,
        )
    except asyncio.TimeoutError:
        pass  # Expected timeout as the task runs in an infinite loop

    assert mock_enqueue.call_count > 0
    
    # Get the message object from the mock call
    message = mock_enqueue.call_args[0][0]
    
    expected_topic = "test/prefix/system/metrics"
    expected_payload = json.dumps(fake_snapshot).encode("utf-8")
    
    assert message.topic_name == expected_topic
    assert message.payload == expected_payload
    assert message.content_type == "application/json"

