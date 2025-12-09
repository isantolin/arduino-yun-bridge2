"""Asyncio task supervision helpers for Yun Bridge."""
from __future__ import annotations

import asyncio
import logging
from builtins import BaseExceptionGroup
from collections.abc import Coroutine
from typing import Any, TypeVar, cast


_T = TypeVar("_T")


class TaskSupervisor:
    """Track background coroutines under a dedicated TaskGroup anchor."""

    def __init__(self, *, logger: logging.Logger | None = None) -> None:
        self._logger = logger or logging.getLogger("yunbridge.tasks")
        self._group: asyncio.TaskGroup | None = None
        self._tasks: set[asyncio.Task[Any]] = set()
        self._lock = asyncio.Lock()

    async def _ensure_group(self) -> asyncio.TaskGroup:
        async with self._lock:
            if self._group is None:
                group = asyncio.TaskGroup()
                await group.__aenter__()
                self._group = group
        group = self._group
        if group is None:  # pragma: no cover - defensive
            raise RuntimeError("TaskGroup initialisation failed")
        return group

    async def start(
        self,
        coroutine: Coroutine[Any, Any, _T],
        *,
        name: str | None = None,
    ) -> asyncio.Task[_T | None]:
        """Schedule *coroutine* and keep track of its lifecycle."""

        group = await self._ensure_group()
        task = group.create_task(
            self._wrap_coroutine(coroutine, name=name),
            name=name,
        )
        task.add_done_callback(self._on_task_done)
        self._tasks.add(task)
        return task

    async def cancel(self) -> None:
        """Cancel all tracked tasks by closing the TaskGroup."""

        async with self._lock:
            group = self._group
            self._group = None

        if group is None:
            return

        try:
            await group.__aexit__(None, None, None)
        except asyncio.CancelledError:  # pragma: no cover - cancellation path
            pass
        except BaseException as exc:  # pragma: no cover - defensive logging
            self._log_group_exception(exc)
        finally:
            self._tasks.clear()

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

    def _wrap_coroutine(
        self,
        coroutine: Coroutine[Any, Any, _T],
        *,
        name: str | None,
    ) -> Coroutine[Any, Any, _T | None]:
        async def runner() -> _T | None:
            try:
                return await coroutine
            except asyncio.CancelledError:
                raise
            except BaseException as exc:
                self._log_task_exception(exc, name=name, coroutine=coroutine)
                return None

        return runner()

    def _log_group_exception(self, exc: BaseException) -> None:
        if isinstance(exc, BaseExceptionGroup):
            group_exc = cast(BaseExceptionGroup, exc)
            exceptions = cast(tuple[BaseException, ...], group_exc.exceptions)
            for inner in exceptions:
                self._log_group_exception(inner)
            return
        self._logger.exception(
            "Background task failed during shutdown",
            exc_info=exc,
        )

    def _log_task_exception(
        self,
        exc: BaseException,
        *,
        name: str | None,
        coroutine: Coroutine[Any, Any, _T],
    ) -> None:
        if isinstance(exc, BaseExceptionGroup):
            group_exc = cast(BaseExceptionGroup, exc)
            exceptions = cast(tuple[BaseException, ...], group_exc.exceptions)
            for inner in exceptions:
                self._log_task_exception(inner, name=name, coroutine=coroutine)
            return
        self._logger.exception(
            "Background task %s failed",
            name or hex(id(coroutine)),
            exc_info=exc,
        )


__all__ = ["TaskSupervisor"]
