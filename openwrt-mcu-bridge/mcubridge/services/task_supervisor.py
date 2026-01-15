"""Asyncio task supervision helpers for MCU Bridge."""

from __future__ import annotations

import asyncio
import logging
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass

from mcubridge.const import (
    SUPERVISOR_DEFAULT_MAX_BACKOFF,
    SUPERVISOR_DEFAULT_MIN_BACKOFF,
    SUPERVISOR_DEFAULT_RESTART_INTERVAL,
    SUPERVISOR_MIN_RESTART_WINDOW,
)
from mcubridge.state.context import RuntimeState


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
    log = logger or logging.getLogger("mcubridge.supervisor")
    current_backoff = min_backoff
    restarts_in_window = 0
    restart_window_duration = max(SUPERVISOR_MIN_RESTART_WINDOW, restart_interval)

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
        except BaseException as exc:
            # [SIL-2] Catch-all for supervisor resilience.
            # We explicitly re-raise control flow exceptions.
            if isinstance(exc, (SystemExit, KeyboardInterrupt, GeneratorExit)):
                raise

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
