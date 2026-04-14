"""Periodic task helper for McuBridge (SIL-2)."""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable

import msgspec

Tick = Callable[[], Awaitable[None]]


async def periodic_task(
    tick: Tick,
    interval: float,
    logger: logging.Logger,
    *,
    log_level: int = logging.DEBUG,
) -> None:
    """Run *tick* repeatedly at *interval* seconds until cancelled.

    Transient errors raised by the callback are caught and logged,
    allowing the next wait/retry cycle to proceed.
    Only ``asyncio.CancelledError`` propagates immediately.
    """
    while True:
        try:
            await tick()
        except asyncio.CancelledError:
            raise
        except (
            OSError,
            RuntimeError,
            ValueError,
            TypeError,
            msgspec.MsgspecError,
        ) as exc:
            logger.log(
                log_level,
                "Periodic task iteration failed (will retry): %s",
                exc,
                exc_info=True,
            )

        await asyncio.sleep(interval)
