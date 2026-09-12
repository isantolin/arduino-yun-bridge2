"""SIL-2 High-Precision Clock Synchronization Service for Arduino MCU Bridge."""

from __future__ import annotations

import asyncio
import time
from typing import TYPE_CHECKING, Any

import structlog

from ..protocol import mcubridge_pb2 as pb
from ..protocol.protocol import Command

if TYPE_CHECKING:
    from .runtime import BridgeService

logger = structlog.get_logger("mcubridge.clock_sync")


class ClockSyncService:
    """Computes round-trip time (RTT), clock offset, and jitter between Linux MPU and MCU. [SIL-2]"""

    def __init__(self, runtime: BridgeService, sync_interval_seconds: float = 30.0) -> None:
        self._runtime: BridgeService = runtime
        self._interval = sync_interval_seconds
        self._task: asyncio.Task[None] | None = None
        self._is_running = False

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

    async def sync_now(self) -> dict[str, Any]:
        """Perform an active clock synchronization exchange with the MCU."""
        serial = self._runtime.serial
        if serial is None or not self._runtime.state.is_connected:
            return self.get_status()

        t1_host_us = time.time_ns() // 1000
        req = pb.ClockSyncRequest(host_time_us=t1_host_us)

        res = await serial.send(Command.CMD_CLOCK_SYNC.value, req)
        if isinstance(res, pb.ClockSyncResponse):
            return self.record_sync(res)
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
            "offset_us": state.clock_offset_us,
            "rtt_us": state.clock_rtt_us,
            "sync_count": state.clock_sync_count,
            "last_sync_unix": state.clock_last_sync_timestamp,
            "is_synchronized": is_conn and state.clock_sync_count > 0,
        }

    async def _sync_loop(self) -> None:
        """Periodic synchronization loop."""
        while self._is_running:
            try:
                if self._runtime.state.is_connected:
                    await self.sync_now()
            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.warning("Periodic clock sync failed", error=str(e))
            await asyncio.sleep(self._interval)
