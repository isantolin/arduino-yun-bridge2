"""Asyncio periodic task helper for McuBridge."""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable

Tick = Callable[[], Awaitable[None]]


async def periodic_task(
    tick: Tick,
    interval: float,
    logger: logging.Logger,
    *,
    log_level: int = logging.DEBUG,
) -> None:
    """Run *tick* repeatedly at *interval* seconds until cancelled.

    Catches all transient errors raised by the callback and logs them.
    Only ``asyncio.CancelledError`` propagates immediately.
    """
    while True:
        try:
            await tick()
        except asyncio.CancelledError:
            raise
        except (RuntimeError, ValueError, OSError, asyncio.TimeoutError) as exc:
            logger.log(log_level, "Periodic task exception: %s", exc)

        await asyncio.sleep(interval)
