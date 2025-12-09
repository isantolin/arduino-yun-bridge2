from __future__ import annotations

import asyncio
import logging
from builtins import ExceptionGroup
from collections.abc import Coroutine
from typing import Any

import pytest

from yunbridge.services.task_supervisor import TaskSupervisor


def test_task_supervisor_tracks_lifecycle(
    caplog: pytest.LogCaptureFixture,
) -> None:
    async def _run() -> None:
        caplog.set_level(logging.DEBUG, logger="test.supervisor")
        supervisor = TaskSupervisor(
            logger=logging.getLogger("test.supervisor")
        )
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


def test_task_supervisor_logs_failures(
    caplog: pytest.LogCaptureFixture,
) -> None:
    async def _run() -> None:
        caplog.set_level(logging.ERROR, logger="test.supervisor")
        supervisor = TaskSupervisor(
            logger=logging.getLogger("test.supervisor")
        )

        async def boom() -> None:
            raise RuntimeError("boom")

        await supervisor.start(boom(), name="boom-task")
        await asyncio.sleep(0)
        await asyncio.sleep(0)
        assert supervisor.active_count == 0
        await supervisor.cancel()

    asyncio.run(_run())
    assert "boom" in caplog.text


def test_task_supervisor_logs_exception_groups(
    caplog: pytest.LogCaptureFixture,
) -> None:
    async def _run() -> None:
        caplog.set_level(logging.ERROR, logger="test.supervisor")
        supervisor = TaskSupervisor(
            logger=logging.getLogger("test.supervisor")
        )

        async def cascaded() -> None:
            raise ExceptionGroup(
                "cascade",
                [RuntimeError("boom-1"), RuntimeError("boom-2")],
            )

        await supervisor.start(cascaded(), name="cascade")
        await asyncio.sleep(0)
        await asyncio.sleep(0)
        await supervisor.cancel()

    asyncio.run(_run())
    assert "boom-1" in caplog.text
    assert "boom-2" in caplog.text


def test_task_supervisor_logs_group_exceptions(
    caplog: pytest.LogCaptureFixture,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class _ExplodingTaskGroup:
        def __init__(self) -> None:
            self._tasks: list[asyncio.Task[None]] = []

        async def __aenter__(self) -> _ExplodingTaskGroup:
            return self

        async def __aexit__(self, exc_type, exc, tb) -> None:
            raise ExceptionGroup(
                "group-closing",
                [RuntimeError("group-boom")],
            )

        def create_task(
            self,
            coroutine: Coroutine[Any, Any, Any],
            *,
            name: str | None = None,
        ) -> asyncio.Task[Any]:
            task = asyncio.create_task(coroutine, name=name)
            self._tasks.append(task)
            return task

    async def _run() -> None:
        caplog.set_level(logging.ERROR, logger="test.supervisor")
        monkeypatch.setattr(
            asyncio,
            "TaskGroup",
            lambda: _ExplodingTaskGroup(),
        )
        supervisor = TaskSupervisor(
            logger=logging.getLogger("test.supervisor")
        )
        await supervisor.start(asyncio.sleep(0))
        await asyncio.sleep(0)
        await supervisor.cancel()

    asyncio.run(_run())
    assert "group-boom" in caplog.text
