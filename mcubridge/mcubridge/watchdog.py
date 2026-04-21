"""Watchdog keepalive utilities for McuBridge."""

from __future__ import annotations

import asyncio
import logging
import os
import time
from collections.abc import Callable

from .config.const import (
    DEFAULT_WATCHDOG_INTERVAL,
    WATCHDOG_MIN_INTERVAL,
    WATCHDOG_TRIGGER_TOKEN,
)
from .state.context import RuntimeState

import functools
import structlog

WatchdogWrite = Callable[[bytes], None]


class WatchdogKeepalive:
    """Emit keepalive pulses for the OpenWrt procd watchdog."""

    # FSM States
    STATE_INIT = "init"
    STATE_RUNNING = "running"
    STATE_STOPPED = "stopped"

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
        self._write = write or functools.partial(os.write, 1)
        self._logger = logger or structlog.get_logger("mcubridge.watchdog")

        # FSM Initialization
        self.fsm_state = self.STATE_INIT

    def trigger(self, event: str) -> None:
        """[SIL-2] Deterministic state transitions without FSM library overhead."""
        old_state = self.fsm_state
        if event == "start":
            if self.fsm_state in (self.STATE_INIT, self.STATE_STOPPED):
                self.fsm_state = self.STATE_RUNNING
        elif event == "stop":
            if self.fsm_state == self.STATE_RUNNING:
                self.fsm_state = self.STATE_STOPPED

        if old_state != self.fsm_state:
            if self.fsm_state == self.STATE_RUNNING:
                self._on_fsm_start()
            elif self.fsm_state == self.STATE_STOPPED:
                self._on_fsm_stop()

    def start(self) -> None:
        self.trigger("start")

    def stop(self) -> None:
        self.trigger("stop")

    def _on_fsm_start(self) -> None:
        """Callback when watchdog starts."""
        self._logger.info("Watchdog keepalive started (interval=%.2fs)", self._interval)

    def _on_fsm_stop(self) -> None:
        """Callback when watchdog stops."""
        self._logger.info("Watchdog keepalive stopped")

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
                # [SIL-2] Direct metrics recording (No Wrapper)
                self._state.watchdog_beats += 1
                self._state.metrics.watchdog_beats.inc()
                self._state.last_watchdog_beat = time.time()

    async def run(self) -> None:
        """Continuously emit watchdog pulses until cancelled."""
        self.start()

        try:
            while True:
                self.kick()
                await asyncio.sleep(self._interval)
        except asyncio.CancelledError:
            self.stop()
            self._logger.debug("Watchdog keepalive cancelled")
            raise


__all__ = ["WatchdogKeepalive"]
