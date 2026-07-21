"""Exhaustive gap closure suite 22 for Python daemon SIL-2 coverage (96%+ target)."""

import asyncio
from unittest.mock import AsyncMock

import pytest
import mcubridge.metrics as metrics_mod
from mcubridge.config.settings import load_runtime_config
from mcubridge.metrics import publish_bridge_snapshots
from mcubridge.state.context import create_runtime_state


@pytest.mark.asyncio
async def test_metrics_cancelled_error_and_disabled_snapshots():
    cfg = load_runtime_config()
    state = create_runtime_state(cfg)
    mock_enqueue = AsyncMock(side_effect=asyncio.CancelledError)
    emit_fn = getattr(metrics_mod, "_emit_bridge_snapshot")

    # 1. _emit_bridge_snapshot with CancelledError
    with pytest.raises(asyncio.CancelledError):
        await emit_fn(state, mock_enqueue, flavor="summary")

    # 2. publish_bridge_snapshots when summary_interval <= 0 and handshake_interval <= 0
    task = asyncio.create_task(
        publish_bridge_snapshots(
            state,
            mock_enqueue,
            summary_interval=0.0,
            handshake_interval=0.0,
            min_interval=0.1,
        )
    )
    await asyncio.sleep(0.05)
    assert not task.done()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
