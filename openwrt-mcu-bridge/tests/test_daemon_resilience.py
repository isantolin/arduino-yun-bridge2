"""Tests for daemon connection resilience and retry logic."""

from __future__ import annotations

import asyncio

import pytest

from mcubridge.services.task_supervisor import supervise_task
from mcubridge.state.context import RuntimeState


@pytest.mark.asyncio
async def test_supervisor_limits_restarts(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    state = RuntimeState()
    attempts = 0
    sleep_calls: list[float] = []

    async def failing_task() -> None:
        nonlocal attempts
        attempts += 1
        raise RuntimeError(f"boom-{attempts}")

    original_sleep = asyncio.sleep

    async def fast_sleep(delay: float) -> None:
        sleep_calls.append(delay)
        await original_sleep(0)

    monkeypatch.setattr("mcubridge.services.task_supervisor.asyncio.sleep", fast_sleep)

    with pytest.raises(RuntimeError, match="boom-3"):
        await asyncio.wait_for(
            supervise_task(
                "unit-test",
                failing_task,
                state=state,
                max_restarts=2,
                restart_interval=5.0,
                min_backoff=0.1,
                max_backoff=0.2,
            ),
            timeout=1,
        )

    stats = state.supervisor_stats["unit-test"]
    assert stats.restarts == 3
    assert len(sleep_calls) == 2
    assert sleep_calls[0] == pytest.approx(0.1)
    assert sleep_calls[1] == pytest.approx(0.2)


@pytest.mark.asyncio
async def test_supervisor_marks_recovery(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    state = RuntimeState()
    attempts = 0

    async def flaky_task() -> None:
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            raise RuntimeError("boom")

    original_sleep = asyncio.sleep

    async def fast_sleep(_: float) -> None:
        await original_sleep(0)

    monkeypatch.setattr("mcubridge.daemon.asyncio.sleep", fast_sleep)

    await asyncio.wait_for(
        supervise_task(
            "unit-test",
            flaky_task,
            state=state,
            max_restarts=5,
            restart_interval=5.0,
            min_backoff=0.1,
            max_backoff=0.1,
        ),
        timeout=1,
    )

    stats = state.supervisor_stats["unit-test"]
    assert stats.restarts == 1
    assert stats.backoff_seconds == 0.0
    assert stats.fatal is False
