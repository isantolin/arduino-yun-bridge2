"""Tests for watchdog keepalive behaviour."""

from __future__ import annotations

import asyncio
import structlog.testing
import contextlib

import pytest
from mcubridge.config.settings import RuntimeConfig
from mcubridge.state.context import RuntimeState, create_runtime_state
from mcubridge.watchdog import WatchdogKeepalive


def test_watchdog_keepalive_emits_pulses(
    runtime_config: RuntimeConfig,
) -> None:
    runtime_config.watchdog_enabled = True
    runtime_config.watchdog_interval = 0.05
    state = create_runtime_state(runtime_config)
    try:
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
    finally:
        state.cleanup()


def test_watchdog_interval_updates(runtime_state: RuntimeState) -> None:
    keepalive = WatchdogKeepalive(state=runtime_state)
    keepalive.update_interval(3.5)
    assert keepalive.interval == 3.5
    keepalive.update_interval(0.2)
    assert keepalive.interval == 0.5


def test_watchdog_kick_handles_write_errors(runtime_state: RuntimeState) -> None:
    """Watchdog should log and continue when the writer fails."""

    def broken_writer(_: bytes) -> None:
        raise OSError("boom")

    keepalive = WatchdogKeepalive(state=runtime_state, write=broken_writer)
    with structlog.testing.capture_logs() as captured:
        keepalive.kick()

    assert runtime_state.watchdog_beats == 0
    assert runtime_state.last_watchdog_beat == 0
    assert any("Failed to emit watchdog trigger" in log["event"] for log in captured)


def test_watchdog_run_logs_cancellation(runtime_state: RuntimeState) -> None:
    runtime_state.watchdog_enabled = True
    runtime_state.watchdog_interval = 0.05

    with structlog.testing.capture_logs() as captured:

        async def _runner() -> None:
            keepalive = WatchdogKeepalive(
                interval=runtime_state.watchdog_interval,
                state=runtime_state,
            )
            task = asyncio.create_task(keepalive.run())
            await asyncio.sleep(0.02)
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await task

        asyncio.run(_runner())
    # [SIL-2] Use case-insensitive search for flexibility
    assert any("keepalive cancelled" in log["event"].lower() for log in captured)
