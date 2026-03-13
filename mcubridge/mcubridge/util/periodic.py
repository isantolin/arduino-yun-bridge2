"""Tenacity-based periodic task helper for McuBridge."""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable

import tenacity

Tick = Callable[[], Awaitable[None]]


async def periodic_task(
    tick: Tick,
    interval: float,
    logger: logging.Logger,
    *,
    log_level: int = logging.DEBUG,
) -> None:
    """Run *tick* repeatedly at *interval* seconds until cancelled.

    Uses ``tenacity`` to space iterations and survive transient errors
    raised by the callback.  Only ``asyncio.CancelledError`` propagates
    immediately — all other exceptions are caught by tenacity and
    trigger the next wait/retry cycle.
    """

    @tenacity.retry(
        wait=tenacity.wait_fixed(interval),
        stop=tenacity.stop_never,
        retry=tenacity.retry_if_not_exception_type(asyncio.CancelledError),
        before_sleep=tenacity.before_sleep_log(logger, log_level),
    )
    async def _loop() -> None:
        await tick()
        raise RuntimeError("tick")

    await _loop()
