from __future__ import annotations

import asyncio
import logging

import pytest

from yunbridge.services.task_supervisor import supervise_task


def test_supervise_task_exits_cleanly(
    caplog: pytest.LogCaptureFixture,
) -> None:
    async def _run() -> None:
        caplog.set_level(logging.WARNING, logger="yunbridge.supervisor")
        
        async def worker() -> None:
            await asyncio.sleep(0.01)

        await supervise_task("clean-worker", worker)

    asyncio.run(_run())
    assert "exited cleanly" in caplog.text


def test_supervise_task_fatal_exception(
    caplog: pytest.LogCaptureFixture,
) -> None:
    async def _run() -> None:
        caplog.set_level(logging.CRITICAL, logger="yunbridge.supervisor")
        
        async def fatal_worker() -> None:
            raise ValueError("fatal error")

        with pytest.raises(ValueError, match="fatal error"):
            await supervise_task(
                "fatal-worker",
                fatal_worker,
                fatal_exceptions=(ValueError,),
            )

    asyncio.run(_run())
    assert "failed with fatal exception" in caplog.text


def test_supervise_task_restarts_on_failure(
    caplog: pytest.LogCaptureFixture,
) -> None:
    async def _run() -> None:
        caplog.set_level(logging.ERROR, logger="yunbridge.supervisor")
        
        attempts = 0
        
        async def flaky_worker() -> None:
            nonlocal attempts
            attempts += 1
            if attempts < 3:
                raise RuntimeError("flaky")
            await asyncio.sleep(0.01)

        # Should restart 2 times and then succeed
        await supervise_task(
            "flaky-worker",
            flaky_worker,
            min_backoff=0.01,
            max_restarts=5,
        )
        
        assert attempts == 3

    asyncio.run(_run())
    assert "failed (flaky); restarting" in caplog.text


def test_supervise_task_max_restarts_exceeded(
    caplog: pytest.LogCaptureFixture,
) -> None:
    async def _run() -> None:
        caplog.set_level(logging.ERROR, logger="yunbridge.supervisor")
        
        async def broken_worker() -> None:
            raise RuntimeError("broken")

        with pytest.raises(RuntimeError, match="broken"):
            await supervise_task(
                "broken-worker",
                broken_worker,
                min_backoff=0.01,
                max_restarts=2,
                restart_interval=0.1, # Short window
            )

    asyncio.run(_run())
    assert "exceeded max restarts" in caplog.text
