"""Asyncio task supervision helpers for Yun Bridge."""
from __future__ import annotations

import asyncio
import logging
from typing import Any, Coroutine, Optional, TypeVar


_T = TypeVar("_T")


class TaskSupervisor:
    """Track background coroutines under a dedicated TaskGroup anchor."""

    def __init__(self, *, logger: Optional[logging.Logger] = None) -> None:
        self._logger = logger or logging.getLogger("yunbridge.tasks")
        self._tasks: set[asyncio.Task[Any]] = set()
        self._lock = asyncio.Lock()

    async def start(
        self,
        coroutine: Coroutine[Any, Any, _T],
        *,
        name: Optional[str] = None,
    ) -> asyncio.Task[_T]:
        """Schedule *coroutine* and keep track of its lifecycle."""

        task: asyncio.Task[_T] = asyncio.create_task(coroutine, name=name)
        task.add_done_callback(self._on_task_done)
        async with self._lock:
            self._tasks.add(task)
        return task

    async def cancel(self) -> None:
        """Cancel all tracked tasks by closing the TaskGroup."""

        async with self._lock:
            if not self._tasks:
                return
            tasks = list(self._tasks)
            self._tasks.clear()

        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)

    @property
    def active_count(self) -> int:
        return len(self._tasks)

    def _on_task_done(self, task: asyncio.Task[Any]) -> None:
        self._tasks.discard(task)
        try:
            task.result()
        except asyncio.CancelledError:
            self._logger.debug(
                "Background task %s cancelled",
                task.get_name() or hex(id(task)),
            )
        except Exception:
            self._logger.exception(
                "Background task %s failed",
                task.get_name() or hex(id(task)),
            )


__all__ = ["TaskSupervisor"]
