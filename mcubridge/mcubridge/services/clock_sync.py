"""SIL-2 High-Precision Clock Synchronization Service for Arduino MCU Bridge."""

from __future__ import annotations

import asyncio
import time
from typing import TYPE_CHECKING, Any

from statemachine import State, StateMachine
import structlog

from ..protocol import mcubridge_pb2 as pb
from ..protocol.protocol import Command

if TYPE_CHECKING:
    from .runtime import BridgeService

logger = structlog.get_logger("mcubridge.clock_sync")


class ClockSyncMachine(StateMachine):
    """[SIL-2] Deterministic state machine for clock synchronization subsystem."""

    idle = State(value="idle", initial=True)
    unsupported = State(value="unsupported")
    probing = State(value="probing")
    synchronized = State(value="synchronized")
    degraded = State(value="degraded")

    mark_unsupported = (
        idle.to(unsupported)
        | probing.to(unsupported)
        | degraded.to(unsupported)
        | synchronized.to(unsupported)
        | unsupported.to(unsupported)
    )
    start_probe = (
        idle.to(probing)
        | unsupported.to(probing)
        | probing.to(probing)
    )
    sync_success = (
        probing.to(synchronized)
        | idle.to(synchronized)
        | degraded.to(synchronized)
        | synchronized.to(synchronized)
        | unsupported.to(synchronized)
    )
    mark_degraded = (
        probing.to(degraded)
        | synchronized.to(degraded)
        | degraded.to(degraded)
    )
    disconnect = (
        probing.to(idle)
        | synchronized.to(idle)
        | degraded.to(idle)
        | unsupported.to(idle)
        | idle.to(idle)
    )
    recheck_capability = unsupported.to(idle)


class ClockSyncService:
    """Computes round-trip time (RTT), clock offset, and jitter between Linux MPU and MCU. [SIL-2]"""

    def __init__(self, runtime: BridgeService, sync_interval_seconds: float = 30.0) -> None:
        self._runtime: BridgeService = runtime
        self._interval = sync_interval_seconds
        self._task: asyncio.Task[None] | None = None
        self._is_running = False
        self.fsm = ClockSyncMachine()

    async def start(self) -> None:
        """Start periodic clock synchronization background worker."""
        if self._is_running:
            return
        self._is_running = True
        self._task = asyncio.create_task(self._sync_loop(), name="clock-sync-worker")

    async def stop(self) -> None:
        """Stop periodic clock synchronization background worker."""
        self._is_running = False
        if self._task and not self._task.done():
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                logger.debug("Clock sync background worker task cancelled")
            self._task = None
        self.fsm.disconnect()

    async def sync_now(self) -> dict[str, Any]:
        """Perform an active clock synchronization exchange with the MCU."""
        serial = self._runtime.serial
        state = self._runtime.state
        if serial is None or not state.is_connected:
            self.fsm.disconnect()
            return self.get_status()

        # [SIL-2] Check MCU capabilities before attempting clock synchronization.
        # Avoid dispatching CMD_CLOCK_SYNC if the MCU firmware explicitly marks it unsupported.
        caps = state.mcu_capabilities
        if caps is not None:
            has_clock_sync = (
                caps.get("clock_sync", None) if isinstance(caps, dict) else getattr(caps, "clock_sync", None)
            )
            if has_clock_sync is False:
                self.fsm.mark_unsupported()
                return self.get_status()

        is_initial_probe = (
            self.fsm.idle.is_active or self.fsm.probing.is_active or self.fsm.unsupported.is_active
        )
        if is_initial_probe:
            self.fsm.start_probe()

        t1_host_us = time.time_ns() // 1000
        req = pb.ClockSyncRequest(host_time_us=t1_host_us)

        # [SIL-2] Bounded response timeout: do not monopolize serial flow lock for 20s.
        res = await serial.send(Command.CMD_CLOCK_SYNC.value, req, timeout=1.0)
        if isinstance(res, pb.ClockSyncResponse):
            return self.record_sync(res)

        if is_initial_probe:
            self.fsm.mark_unsupported()
        else:
            self.fsm.mark_degraded()
        return self.get_status()

    def record_sync(self, resp: pb.ClockSyncResponse) -> dict[str, Any]:
        """Process incoming ClockSyncResponse from MCU and update telemetry."""
        t4_host_us = time.time_ns() // 1000
        t1_host_us = resp.host_time_us
        t2_mcu_us = resp.mcu_time_us

        rtt_us = t4_host_us - t1_host_us
        offset_us = t2_mcu_us - (t1_host_us + (rtt_us // 2))

        state = self._runtime.state
        state.clock_offset_us = offset_us
        state.clock_rtt_us = rtt_us
        state.clock_sync_count += 1
        state.clock_last_sync_timestamp = time.time()

        self.fsm.sync_success()

        logger.info(
            "Clock synchronization updated",
            rtt_us=rtt_us,
            offset_us=offset_us,
            mcu_time_us=t2_mcu_us,
            sync_count=state.clock_sync_count,
        )
        return self.get_status()

    def get_status(self) -> dict[str, Any]:
        """Retrieve current clock synchronization metrics."""
        state = self._runtime.state
        is_conn = self._runtime.serial is not None and state.is_connected
        return {
            "state": self.fsm.current_state_value,
            "offset_us": state.clock_offset_us,
            "rtt_us": state.clock_rtt_us,
            "sync_count": state.clock_sync_count,
            "last_sync_unix": state.clock_last_sync_timestamp,
            "is_synchronized": is_conn and self.fsm.synchronized.is_active,
        }

    async def _sync_loop(self) -> None:
        """Periodic synchronization loop."""
        while self._is_running:
            try:
                if self._runtime.state.is_connected:
                    if not self.fsm.unsupported.is_active:
                        await self.sync_now()
                else:
                    self.fsm.disconnect()
            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.warning("Periodic clock sync failed", error=str(e))
                if self.fsm.idle.is_active or self.fsm.probing.is_active:
                    self.fsm.mark_unsupported()
                else:
                    self.fsm.mark_degraded()
            await asyncio.sleep(self._interval)
