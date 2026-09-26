"""Watchdog keepalive utilities for McuBridge (SIL-2)."""

from __future__ import annotations

import asyncio
import os
import time
from collections.abc import Callable
from enum import StrEnum

import structlog
from statemachine import State, StateMachine

from .config.const import (
    WATCHDOG_MIN_INTERVAL,
    WATCHDOG_TRIGGER_TOKEN,
)
from .protocol import protocol
from .state.context import RuntimeState

WatchdogWrite = Callable[[bytes], None]

logger = structlog.get_logger("mcubridge.watchdog")


class WatchdogSupervisorState(StrEnum):
    """[SIL-2] Discrete states for the watchdog supervisor."""

    INITIALIZING = "initializing"
    HEALTHY = "healthy"
    DEGRADED = "degraded"
    CRITICAL_INHIBIT = "critical_inhibit"
    SHUTDOWN = "shutdown"


class WatchdogSupervisorMachine(StateMachine):
    """[SIL-2] Strongly-typed deterministic FSM for watchdog supervisor."""

    allow_event_without_transition = True

    initializing = State(value="initializing", initial=True)
    healthy = State(value="healthy")
    degraded = State(value="degraded")
    critical_inhibit = State(value="critical_inhibit")
    shutdown = State(value="shutdown")

    start_healthy = initializing.to(healthy) | degraded.to(healthy) | healthy.to(healthy)
    degrade = healthy.to(degraded) | initializing.to(degraded) | degraded.to(degraded)
    recover = degraded.to(healthy) | healthy.to(healthy)
    trip_inhibit = (
        healthy.to(critical_inhibit)
        | degraded.to(critical_inhibit)
        | initializing.to(critical_inhibit)
        | critical_inhibit.to(critical_inhibit)
    )
    stop = (
        healthy.to(shutdown)
        | degraded.to(shutdown)
        | critical_inhibit.to(shutdown)
        | initializing.to(shutdown)
        | shutdown.to(shutdown)
    )


class WatchdogKeepalive:
    """Emit keepalive pulses for the OpenWrt procd watchdog governed by SIL-2 FSM."""

    def __init__(
        self,
        *,
        interval: float = protocol.DEFAULT_WATCHDOG_INTERVAL,
        state: RuntimeState | None = None,
        token: bytes = WATCHDOG_TRIGGER_TOKEN,
        write: WatchdogWrite | None = None,
    ) -> None:
        self._interval = max(WATCHDOG_MIN_INTERVAL, interval)
        self._state = state
        self._token = token
        self.status = WatchdogSupervisorState.INITIALIZING.value
        self.fsm = WatchdogSupervisorMachine(model=self, state_field="status", start_value=self.status)

        def default_write(b: bytes) -> None:
            os.write(1, b)

        self._write = write or default_write

    @property
    def interval(self) -> float:
        return self._interval

    @interval.setter
    def interval(self, value: float) -> None:
        self._interval = max(WATCHDOG_MIN_INTERVAL, value)

    def is_healthy(self) -> bool:
        """Check if supervisor state allows watchdog keepalive pulse emission."""
        if self.fsm.critical_inhibit.is_active or self.fsm.shutdown.is_active:
            return False
        if self._state is not None and getattr(self._state, "fatal_count", 0) > 0:
            # If the link has fatal failures recorded, trip inhibit immediately
            if not self.fsm.critical_inhibit.is_active:
                self.trip_inhibit("MCU link fatal reset detected")
            return False
        return True

    def trip_inhibit(self, reason: str = "") -> None:
        """Deterministically inhibit watchdog pulses to trigger supervisor recovery."""
        if not self.fsm.critical_inhibit.is_active:
            self.fsm.trip_inhibit()
            logger.critical("Watchdog pulse inhibited by supervisor", reason=reason, state=self.status)

    def degrade(self, reason: str = "") -> None:
        """Transition supervisor to degraded state while still maintaining keepalive."""
        if self.fsm.healthy.is_active or self.fsm.initializing.is_active:
            self.fsm.degrade()
            logger.warning("Watchdog supervisor degraded", reason=reason)

    def recover(self) -> None:
        """Transition supervisor back to healthy state."""
        if self.fsm.degraded.is_active:
            self.fsm.recover()
            logger.info("Watchdog supervisor recovered to healthy state")

    def kick(self) -> None:
        """Send a single watchdog pulse immediately if supervisor state is healthy/degraded."""
        if not self.is_healthy():
            logger.warning("Watchdog kick inhibited due to supervisor state", state=self.status)
            return

        if self.fsm.initializing.is_active:
            self.fsm.start_healthy()

        try:
            self._write(self._token)
        except OSError as exc:
            logger.warning("Failed to emit watchdog trigger", error=str(exc))
            self.degrade(str(exc))
        else:
            if self.fsm.degraded.is_active:
                self.recover()
            if self._state is not None:
                # [SIL-2] Direct metrics recording
                self._state.watchdog_beats += 1
                self._state.metrics.watchdog_beats.inc()
                self._state.last_watchdog_beat = time.time()

    async def run(self) -> None:
        """Continuously emit watchdog pulses until cancelled."""
        logger.info("Watchdog keepalive started", interval_seconds=self.interval)

        try:
            while not self.fsm.shutdown.is_active:
                self.kick()
                await asyncio.sleep(self.interval)
        except asyncio.CancelledError:
            self.fsm.stop()
            logger.info("Watchdog keepalive cancelled")
            raise
