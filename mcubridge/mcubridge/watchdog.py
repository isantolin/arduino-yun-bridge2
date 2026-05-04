"""Watchdog keepalive utilities for McuBridge (SIL-2)."""

from __future__ import annotations

import asyncio
import os
import time
from collections.abc import Callable

import structlog

from .config.const import (
    DEFAULT_WATCHDOG_INTERVAL,
    WATCHDOG_MIN_INTERVAL,
    WATCHDOG_TRIGGER_TOKEN,
)
from .state.context import RuntimeState

WatchdogWrite = Callable[[bytes], None]

logger = structlog.get_logger("mcubridge.watchdog")


class WatchdogKeepalive:
    """Emit keepalive pulses for the OpenWrt procd watchdog."""

    def __init__(
        self,
        *,
        interval: float = DEFAULT_WATCHDOG_INTERVAL,
        state: RuntimeState | None = None,
        token: bytes = WATCHDOG_TRIGGER_TOKEN,
        write: WatchdogWrite | None = None,
    ) -> None:
        self._interval = max(WATCHDOG_MIN_INTERVAL, interval)
        self._state = state
        self._token = token

        def default_write(b: bytes) -> None:
            os.write(1, b)

        self._write = write or default_write

    @property
    def interval(self) -> float:
        return self._interval

    @interval.setter
    def interval(self, value: float) -> None:
        self._interval = max(WATCHDOG_MIN_INTERVAL, value)

    def update_interval(self, interval: float) -> None:
        """Compatibility shim for tests."""
        self.interval = interval

    def kick(self) -> None:
        """Send a single watchdog pulse immediately."""
        try:
            self._write(self._token)
        except OSError as exc:
            logger.warning("Failed to emit watchdog trigger: %s", exc)
        else:
            if self._state is not None:
                # [SIL-2] Direct metrics recording
                self._state.watchdog_beats += 1
                self._state.metrics.watchdog_beats.inc()
                self._state.last_watchdog_beat = time.time()

    async def run(self) -> None:
        """Continuously emit watchdog pulses until cancelled."""
        logger.info("Watchdog keepalive started (interval=%.2fs)", self.interval)

        try:
            while True:
                self.kick()
                await asyncio.sleep(self.interval)
        except asyncio.CancelledError:
            logger.info("Watchdog keepalive cancelled")
            raise


__all__ = ["WatchdogKeepalive"]
