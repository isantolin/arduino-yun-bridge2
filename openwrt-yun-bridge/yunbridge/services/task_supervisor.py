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
        self._group: Optional[asyncio.TaskGroup] = None
        self._anchor: Optional[asyncio.Task[None]] = None
        self._group_ready: Optional[asyncio.Event] = None
        self._shutdown_event: Optional[asyncio.Event] = None
        self._lock = asyncio.Lock()

    async def start(
        self,
        coroutine: Coroutine[Any, Any, _T],
        *,
        name: Optional[str] = None,
    ) -> asyncio.Task[_T]:
        """Schedule *coroutine* and keep track of its lifecycle."""

        group = await self._acquire_group()
        task: asyncio.Task[_T] = group.create_task(coroutine, name=name)
        self._tasks.add(task)
        task.add_done_callback(self._on_task_done)
        return task

    async def cancel(self) -> None:
        """Cancel all tracked tasks by shutting down the TaskGroup anchor."""

        anchor: Optional[asyncio.Task[None]]
        async with self._lock:
            anchor = self._anchor
            shutdown = self._shutdown_event
            if anchor is None:
                return
            if shutdown is not None and not shutdown.is_set():
                shutdown.set()

        try:
            await anchor
        finally:
            async with self._lock:
                if self._anchor is anchor:
                    self._anchor = None
            self._tasks.clear()

    @property
    def active_count(self) -> int:
        return len(self._tasks)

    async def _acquire_group(self) -> asyncio.TaskGroup:
        group = self._group
        if group is not None:
            return group

        await self._ensure_anchor()
        assert self._group is not None
        return self._group

    async def _ensure_anchor(self) -> None:
        while True:
            async with self._lock:
                anchor = self._anchor
                ready = self._group_ready
                group = self._group
                anchor_running = anchor is not None and not anchor.done()

                if group is not None and anchor_running and ready is not None:
                    wait_event = ready
                else:
                    if not anchor_running:
                        self._group_ready = asyncio.Event()
                        self._shutdown_event = asyncio.Event()
                        self._anchor = asyncio.create_task(self._run_anchor())
                        wait_event = self._group_ready
                    else:
                        wait_event = ready

            if wait_event is None:
                await asyncio.sleep(0)
                continue

            await wait_event.wait()
            if self._group is not None:
                return

    async def _run_anchor(self) -> None:
        ready = self._group_ready
        shutdown = self._shutdown_event
        if ready is None or shutdown is None:
            return

        try:
            async with asyncio.TaskGroup() as group:
                self._group = group
                ready.set()
                await shutdown.wait()
        except ExceptionGroup:
            self._logger.exception(
                "Background task group terminated with errors"
            )
        except asyncio.CancelledError:
            raise
        except BaseException:
            self._logger.exception(
                "Background task group terminated unexpectedly"
            )
        finally:
            self._group = None
            if self._group_ready is ready:
                self._group_ready = None
            if self._shutdown_event is shutdown:
                self._shutdown_event = None
            async with self._lock:
                if self._anchor is asyncio.current_task():
                    self._anchor = None

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
