"""Asyncio task supervision helpers for Yun Bridge."""

from __future__ import annotations

import asyncio
import logging
import time
from builtins import BaseExceptionGroup
from collections.abc import Awaitable, Callable, Coroutine
from dataclasses import dataclass
from typing import Any, TypeVar, Self, cast

from yunbridge.const import (
    SUPERVISOR_DEFAULT_MAX_BACKOFF,
    SUPERVISOR_DEFAULT_MIN_BACKOFF,
    SUPERVISOR_DEFAULT_RESTART_INTERVAL,
)
from yunbridge.state.context import RuntimeState

_T = TypeVar("_T")


@dataclass(slots=True)
class SupervisedTaskSpec:
    name: str
    factory: Callable[[], Awaitable[None]]
    fatal_exceptions: tuple[type[BaseException], ...] = ()
    max_restarts: int | None = None
    restart_interval: float = SUPERVISOR_DEFAULT_RESTART_INTERVAL
    min_backoff: float = SUPERVISOR_DEFAULT_MIN_BACKOFF
    max_backoff: float = SUPERVISOR_DEFAULT_MAX_BACKOFF


async def supervise_task(
    name: str,
    coro_factory: Callable[[], Awaitable[None]],
    *,
    fatal_exceptions: tuple[type[BaseException], ...] = (),
    min_backoff: float = SUPERVISOR_DEFAULT_MIN_BACKOFF,
    max_backoff: float = SUPERVISOR_DEFAULT_MAX_BACKOFF,
    state: RuntimeState | None = None,
    max_restarts: int | None = None,
    restart_interval: float = SUPERVISOR_DEFAULT_RESTART_INTERVAL,
    logger: logging.Logger | None = None,
) -> None:
    """Run *coro_factory* restarting it on failures using native loops."""
    log = logger or logging.getLogger("yunbridge.supervisor")
    current_backoff = min_backoff
    restarts_in_window = 0
    restart_window_duration = max(1.0, restart_interval)

    while True:
        start_time = time.monotonic()
        try:
            # Reset backoff on successful start (if it runs for a while, logic below handles crashes)
            await coro_factory()

            # If we get here, the task exited cleanly.
            log.warning(
                "%s task exited cleanly; supervisor exiting",
                name,
            )
            if state is not None:
                state.mark_supervisor_healthy(name)
            return

        except asyncio.CancelledError:
            log.debug("%s supervisor cancelled", name)
            raise
        except fatal_exceptions as exc:
            log.critical(
                "%s failed with fatal exception: %s",
                name,
                exc,
            )
            if state is not None:
                state.record_supervisor_failure(
                    name, backoff=0.0, exc=exc, fatal=True
                )
            raise
        except Exception as exc:
            now = time.monotonic()
            runtime = now - start_time

            if runtime > restart_window_duration:
                # Task ran long enough to be considered healthy
                restarts_in_window = 0
                current_backoff = min_backoff
                if state is not None:
                    state.mark_supervisor_healthy(name)

            restarts_in_window += 1

            if max_restarts is not None and restarts_in_window > max_restarts:
                log.error(
                    "%s exceeded max restarts (%d) in window; giving up",
                    name,
                    max_restarts,
                )
                if state is not None:
                    state.record_supervisor_failure(
                        name, backoff=current_backoff, exc=exc, fatal=True
                    )
                raise

            log.error(
                "%s failed (%s); restarting in %.1fs",
                name,
                exc,
                current_backoff,
            )

            if state is not None:
                state.record_supervisor_failure(
                    name, backoff=current_backoff, exc=exc
                )

            try:
                await asyncio.sleep(current_backoff)
            except asyncio.CancelledError:
                raise

            current_backoff = min(current_backoff * 2, max_backoff)


class TaskSupervisor:
    """Track background coroutines under a dedicated TaskGroup anchor."""

    def __init__(self, *, logger: logging.Logger | None = None) -> None:
        self._logger = logger or logging.getLogger("yunbridge.tasks")
        self._group: asyncio.TaskGroup | None = None
        self._tasks: set[asyncio.Task[Any]] = set()

    async def __aenter__(self) -> Self:
        self._group = asyncio.TaskGroup()
        await self._group.__aenter__()
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc_val: BaseException | None,
        exc_tb: Any,
    ) -> None:
        if self._group:
            # Cancel all tracked tasks to ensure we don't hang on exit
            for task in self._tasks:
                if not task.done():
                    task.cancel()

            try:
                await self._group.__aexit__(exc_type, exc_val, exc_tb)
            except Exception as exc:
                # Log the exception before propagating
                self._log_task_exception(exc, name="TaskGroup")
                # Propagate exceptions from the group
                raise
            finally:
                self._group = None
                self._tasks.clear()

    def start(
        self,
        coroutine: Coroutine[Any, Any, _T],
        *,
        name: str | None = None,
    ) -> asyncio.Task[_T | None]:
        """Schedule *coroutine* and keep track of its lifecycle."""
        if self._group is None:
            raise RuntimeError("TaskSupervisor context not entered")

        task = self._group.create_task(
            self._wrap_coroutine(coroutine, name=name),
            name=name,
        )
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)
        return task

    @property
    def active_count(self) -> int:
        """Return the number of currently tracked tasks."""
        return len(self._tasks)

    async def cancel(self) -> None:
        """Cancel all tracked tasks."""
        for task in self._tasks:
            if not task.done():
                task.cancel()

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

    def _log_task_exception(
        self,
        exc: BaseException,
        *,
        name: str | None,
        coroutine: Coroutine[Any, Any, _T] | None = None,
    ) -> None:
        if isinstance(exc, BaseExceptionGroup):
            group_exc = cast(BaseExceptionGroup, exc)
            exceptions = cast(tuple[BaseException, ...], group_exc.exceptions)
            for inner in exceptions:
                self._log_task_exception(inner, name=name, coroutine=coroutine)
            return
        self._logger.exception(
            "Background task %s failed",
            name or (hex(id(coroutine)) if coroutine else "unknown"),
            exc_info=exc,
        )


__all__ = ["TaskSupervisor"]
