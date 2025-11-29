from __future__ import annotations

import asyncio
import logging

import pytest

from yunbridge.services.task_supervisor import TaskSupervisor


def test_task_supervisor_tracks_lifecycle(caplog: pytest.LogCaptureFixture) -> None:
    async def _run() -> None:
        caplog.set_level(logging.DEBUG, logger="test.supervisor")
        supervisor = TaskSupervisor(logger=logging.getLogger("test.supervisor"))
        completed = asyncio.Event()

        async def worker() -> None:
            await asyncio.sleep(0)
            completed.set()

        await supervisor.start(worker(), name="worker")
        await asyncio.wait_for(completed.wait(), timeout=1)
        await asyncio.sleep(0)
        assert supervisor.active_count == 0
        await supervisor.cancel()

    asyncio.run(_run())


def test_task_supervisor_logs_failures(caplog: pytest.LogCaptureFixture) -> None:
    async def _run() -> None:
        caplog.set_level(logging.ERROR, logger="test.supervisor")
        supervisor = TaskSupervisor(logger=logging.getLogger("test.supervisor"))

        async def boom() -> None:
            raise RuntimeError("boom")

        await supervisor.start(boom(), name="boom-task")
        await asyncio.sleep(0)
        await asyncio.sleep(0)
        assert supervisor.active_count == 0
        await supervisor.cancel()

    asyncio.run(_run())
    assert "boom" in caplog.text
