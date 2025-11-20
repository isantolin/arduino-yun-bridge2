"""Tests for watchdog keepalive behaviour."""
from __future__ import annotations

import asyncio

import pytest

from yunbridge.config.settings import RuntimeConfig
from yunbridge.state.context import RuntimeState, create_runtime_state
from yunbridge.watchdog import WatchdogKeepalive


def test_watchdog_keepalive_emits_pulses(
    runtime_config: RuntimeConfig,
) -> None:
    runtime_config.watchdog_enabled = True
    runtime_config.watchdog_interval = 0.05
    state = create_runtime_state(runtime_config)

    pulses: list[bytes] = []

    def capture(data: bytes) -> None:
        pulses.append(data)

    async def _runner() -> None:
        keepalive = WatchdogKeepalive(
            interval=runtime_config.watchdog_interval,
            state=state,
            write=capture,
        )

        task = asyncio.create_task(keepalive.run())
        await asyncio.sleep(0.12)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

    asyncio.run(_runner())

    assert len(pulses) >= 1
    assert state.watchdog_beats == len(pulses)
    assert state.last_watchdog_beat > 0


def test_watchdog_interval_updates(runtime_state: RuntimeState) -> None:
    keepalive = WatchdogKeepalive(state=runtime_state)
    keepalive.update_interval(3.5)
    assert keepalive.interval == 3.5
    keepalive.update_interval(0.2)
    assert keepalive.interval == 0.5
