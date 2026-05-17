"""Watchdog tests with strict typing."""

from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING
from unittest.mock import patch

import pytest

from mcubridge.watchdog import WatchdogKeepalive

if TYPE_CHECKING:
    from mcubridge.config.settings import RuntimeConfig
    from mcubridge.state.context import RuntimeState


def test_watchdog_keepalive_emits_pulses(
    runtime_config: RuntimeConfig,
    runtime_state: RuntimeState,
) -> None:
    """Verify watchdog emits pulses at the configured interval."""
    runtime_config.watchdog_enabled = True
    runtime_config.watchdog_interval = 0.05
    pulses: list[bytes] = []

    def capture(data: bytes) -> None:
        pulses.append(data)

    async def _runner() -> None:
        keepalive = WatchdogKeepalive(
            interval=runtime_config.watchdog_interval,
            state=runtime_state,
            write=capture,
        )

        task = asyncio.create_task(keepalive.run())
        await asyncio.sleep(0.12)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

    asyncio.run(_runner())

    assert len(pulses) >= 1
    assert runtime_state.watchdog_beats == len(pulses)
    assert runtime_state.last_watchdog_beat > 0


def test_watchdog_interval_updates(runtime_state: RuntimeState) -> None:
    """Verify interval updates and clamping."""
    keepalive = WatchdogKeepalive(state=runtime_state)
    keepalive.interval = 3.5
    assert keepalive.interval == 3.5
    keepalive.interval = 0.2
    assert keepalive.interval == 0.5


def test_watchdog_kick_handles_write_errors(runtime_state: RuntimeState) -> None:
    """Watchdog should log and continue when the writer fails."""

    def broken_writer(_: bytes) -> None:
        raise OSError("boom")

    keepalive = WatchdogKeepalive(state=runtime_state, write=broken_writer)
    with patch("mcubridge.watchdog.logger") as mock_logger:
        keepalive.kick()

    assert runtime_state.watchdog_beats == 0
    assert runtime_state.last_watchdog_beat == 0
    assert mock_logger.warning.called
    assert "Failed to emit watchdog trigger" in mock_logger.warning.call_args[0][0]


def test_watchdog_run_logs_cancellation(runtime_state: RuntimeState) -> None:
    """Verify cancellation is logged."""
    runtime_state.watchdog_enabled = True
    runtime_state.watchdog_interval = 0.05

    with patch("mcubridge.watchdog.logger") as mock_logger:

        async def _runner() -> None:
            keepalive = WatchdogKeepalive(
                interval=runtime_state.watchdog_interval,
                state=runtime_state,
            )
            task = asyncio.create_task(keepalive.run())
            await asyncio.sleep(0.05)
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task

        asyncio.run(_runner())

    assert any("keepalive cancelled" in str(call.args[0]) for call in mock_logger.info.mock_calls)
