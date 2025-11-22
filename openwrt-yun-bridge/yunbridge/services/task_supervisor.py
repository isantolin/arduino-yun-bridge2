"""Asyncio task supervision helpers for Yun Bridge."""
from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable
from typing import Any, Optional


class TaskSupervisor:
    """Track background coroutines and surface failures centrally."""

    def __init__(self, *, logger: Optional[logging.Logger] = None) -> None:
        self._logger = logger or logging.getLogger("yunbridge.tasks")
        self._tasks: set[asyncio.Task[Any]] = set()

    def start(
        self,
        coroutine: Awaitable[Any],
        *,
        name: Optional[str] = None,
    ) -> asyncio.Task[Any]:
        """Schedule *coroutine* and capture its lifespan."""

        loop = asyncio.get_running_loop()
        task = loop.create_task(coroutine, name=name)
        self._tasks.add(task)
        task.add_done_callback(self._on_task_done)
        return task

    async def cancel(self) -> None:
        """Cancel all tracked tasks and wait for their completion."""

        if not self._tasks:
            return
        tasks = list(self._tasks)
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        self._tasks.difference_update(tasks)

    @property
    def active_count(self) -> int:
        return len(self._tasks)

    def _on_task_done(self, task: asyncio.Task[Any]) -> None:
        self._tasks.discard(task)
        try:
            task.result()
        except asyncio.CancelledError:
            self._logger.debug(
                "Background task %s cancelled", task.get_name() or hex(id(task))
            )
        except Exception:
            self._logger.exception(
                "Background task %s failed", task.get_name() or hex(id(task))
            )


__all__ = ["TaskSupervisor"]
