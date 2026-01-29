"""Asyncio task supervision helpers for MCU Bridge."""

from __future__ import annotations

import asyncio
import logging
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass

import tenacity

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


class _SupervisorRetryState:
    """Helper to track supervisor health and logging across tenacity retries."""

    def __init__(
        self,
        name: str,
        log: logging.Logger,
        state: RuntimeState | None,
        window: float,
    ) -> None:
        self.name = name
        self.log = log
        self.state = state
        self.window = window
        self.last_start_time = 0.0

    def mark_started(self) -> None:
        self.last_start_time = time.monotonic()

    def is_healthy_runtime(self) -> bool:
        if self.last_start_time <= 0:
            return False
        return (time.monotonic() - self.last_start_time) > self.window

    def before_sleep(self, retry_state: tenacity.RetryCallState) -> None:
        exc = retry_state.outcome.exception() if retry_state.outcome else None
        delay = retry_state.next_action.sleep if retry_state.next_action else 0.0
        self.log.error("%s failed (%s); restarting in %.1fs", self.name, exc, delay)

    def after(self, retry_state: tenacity.RetryCallState) -> None:
        exc = retry_state.outcome.exception() if retry_state.outcome else None
        if self.state is not None and exc:
            # If next_action is None, tenacity has stopped retrying
            is_last = retry_state.next_action is None
            delay = retry_state.next_action.sleep if retry_state.next_action else 0.0
            self.state.record_supervisor_failure(self.name, backoff=delay, exc=exc, fatal=is_last)


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
    """Run *coro_factory* restarting it on failures using tenacity."""
    log = logger or logging.getLogger("mcubridge.supervisor")
    restart_window_duration = max(SUPERVISOR_MIN_RESTART_WINDOW, restart_interval)

    helper = _SupervisorRetryState(name, log, state, restart_window_duration)

    retryer = tenacity.AsyncRetrying(
        wait=tenacity.wait_exponential(multiplier=min_backoff, max=max_backoff),
        retry=tenacity.retry_if_not_exception_type(
            (asyncio.CancelledError, SystemExit, KeyboardInterrupt, GeneratorExit) + fatal_exceptions
        ),
        stop=tenacity.stop_after_attempt(max_restarts + 1) if max_restarts is not None else tenacity.stop_never,
        before_sleep=helper.before_sleep,
        after=helper.after,
        reraise=True,
    )

    try:
        while True:
            try:
                async for attempt in retryer:
                    with attempt:
                        helper.mark_started()
                        await coro_factory()

                        # If we get here, the task exited cleanly.
                        log.warning("%s task exited cleanly; supervisor exiting", name)
                        if state is not None:
                            state.mark_supervisor_healthy(name)
                        return
            except tenacity.RetryError:
                # This block is only entered if reraise=False (not our case)
                log.error("%s exceeded max restarts (%s); giving up", name, max_restarts)
                raise
            except fatal_exceptions as exc:
                log.critical("%s failed with fatal exception: %s", name, exc)
                if state is not None:
                    state.record_supervisor_failure(name, backoff=0.0, exc=exc, fatal=True)
                raise
            except BaseException:
                # If reraise=True, the last attempt's exception will pop out here
                # when stop_after_attempt is reached.
                if helper.is_healthy_runtime():
                    log.info("%s was healthy long enough; resetting backoff", name)
                    if state is not None:
                        state.mark_supervisor_healthy(name)
                    continue

                # Check if we gave up
                if max_restarts is not None and retryer.statistics["attempt_number"] >= (max_restarts + 1):
                    log.error("%s exceeded max restarts (%d) in window; giving up", name, max_restarts)

                raise
    except asyncio.CancelledError:
        log.debug("%s supervisor cancelled", name)
        raise
