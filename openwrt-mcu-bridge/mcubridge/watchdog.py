"""Watchdog keepalive utilities for McuBridge."""

from __future__ import annotations

import asyncio
import logging
import os
import time
from collections.abc import Callable

from .config.const import DEFAULT_WATCHDOG_INTERVAL, WATCHDOG_MIN_INTERVAL, WATCHDOG_TRIGGER_TOKEN
from .state.context import RuntimeState

WatchdogWrite = Callable[[bytes], None]


def _default_write(payload: bytes) -> None:
    os.write(1, payload)


class WatchdogKeepalive:
    """Emit keepalive pulses for the OpenWrt procd watchdog."""

    def __init__(
        self,
        *,
        interval: float = DEFAULT_WATCHDOG_INTERVAL,
        state: RuntimeState | None = None,
        token: bytes = WATCHDOG_TRIGGER_TOKEN,
        write: WatchdogWrite | None = None,
        logger: logging.Logger | None = None,
    ) -> None:
        self._interval = max(WATCHDOG_MIN_INTERVAL, interval)
        self._state = state
        self._token = token
        self._write = write or _default_write
        self._logger = logger or logging.getLogger("mcubridge.watchdog")

    @property
    def interval(self) -> float:
        return self._interval

    def update_interval(self, interval: float) -> None:
        """Update the keepalive interval at runtime.

        This method is primarily exposed for testing scenarios where the
        watchdog timing needs to be adjusted dynamically. In production,
        the interval is typically set once at instantiation.

        Args:
            interval: New interval in seconds (clamped to WATCHDOG_MIN_INTERVAL).
        """
        self._interval = max(WATCHDOG_MIN_INTERVAL, interval)

    def kick(self) -> None:
        """Send a single watchdog pulse immediately."""
        try:
            self._write(self._token)
        except OSError as exc:
            self._logger.warning("Failed to emit watchdog trigger: %s", exc)
        else:
            if self._state is not None:
                self._state.record_watchdog_beat(time.monotonic())

    async def run(self) -> None:
        """Continuously emit watchdog pulses until cancelled."""
        try:
            while True:
                self.kick()
                await asyncio.sleep(self._interval)
        except asyncio.CancelledError:
            self._logger.debug("Watchdog keepalive cancelled")
            raise


__all__ = ["WatchdogKeepalive"]
