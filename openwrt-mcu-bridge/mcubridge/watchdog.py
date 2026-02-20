"""Watchdog keepalive utilities for McuBridge."""

from __future__ import annotations

import asyncio
import logging
import os
import time
from collections.abc import Callable
from typing import TYPE_CHECKING

from transitions import Machine

from .config.const import DEFAULT_WATCHDOG_INTERVAL, WATCHDOG_MIN_INTERVAL, WATCHDOG_TRIGGER_TOKEN
from .state.context import RuntimeState

WatchdogWrite = Callable[[bytes], None]


def _default_write(payload: bytes) -> None:
    os.write(1, payload)


class WatchdogKeepalive:
    """Emit keepalive pulses for the OpenWrt procd watchdog."""

    if TYPE_CHECKING:
        # FSM generated methods and attributes for static analysis
        fsm_state: str
        start: Callable[[], None]
        stop: Callable[[], None]

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
        self._write = write or _default_write
        self._logger = logger or logging.getLogger("mcubridge.watchdog")

        # FSM Initialization
        self.state_machine = Machine(
            model=self,
            states=[
                self.STATE_INIT,
                self.STATE_RUNNING,
                self.STATE_STOPPED
            ],
            initial=self.STATE_INIT,
            ignore_invalid_triggers=True,
            model_attribute='fsm_state'
        )

        # FSM Transitions
        self.state_machine.add_transition(trigger='start', source=[self.STATE_INIT, self.STATE_STOPPED], dest=self.STATE_RUNNING)
        self.state_machine.add_transition(trigger='stop', source=self.STATE_RUNNING, dest=self.STATE_STOPPED)

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
