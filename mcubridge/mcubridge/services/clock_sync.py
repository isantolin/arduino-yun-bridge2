"""SIL-2 High-Precision Clock Synchronization Service for Arduino MCU Bridge."""

from __future__ import annotations

import asyncio
import time
from typing import Any, Protocol

import structlog
from google.protobuf.message import DecodeError as ProtobufDecodeError
from statemachine import State, StateMachine

from mcubridge.state.context import RuntimeState
from ..protocol import mcubridge_pb2 as pb
from ..protocol.protocol import Command


class ClockSyncRuntime(Protocol):
    """Protocol for runtime link dependency in ClockSyncService."""

    @property
    def serial(self) -> Any: ...
    @property
    def state(self) -> RuntimeState: ...


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
    start_probe = idle.to(probing) | unsupported.to(probing) | probing.to(probing)
    sync_success = (
        probing.to(synchronized)
        | idle.to(synchronized)
        | degraded.to(synchronized)
        | synchronized.to(synchronized)
        | unsupported.to(synchronized)
    )
    mark_degraded = probing.to(degraded) | synchronized.to(degraded) | degraded.to(degraded)
    disconnect = probing.to(idle) | synchronized.to(idle) | degraded.to(idle) | unsupported.to(idle) | idle.to(idle)


class ClockSyncService:
    """Computes round-trip time (RTT), clock offset, and jitter between Linux MPU and MCU. [SIL-2]"""

    def __init__(self, runtime: ClockSyncRuntime, sync_interval_seconds: float = 30.0) -> None:
        self._runtime: ClockSyncRuntime = runtime
        self._interval: float = sync_interval_seconds
        self.fsm = ClockSyncMachine()

    async def run(self) -> None:
        """Run periodic clock synchronization worker within supervisor TaskGroup. [SIL-2]"""
        try:
            await self._sync_loop()
        finally:
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

        is_initial_probe = self.fsm.idle.is_active or self.fsm.probing.is_active or self.fsm.unsupported.is_active
        if is_initial_probe:
            self.fsm.start_probe()

        t1_host_us = time.time_ns() // 1000
        req = pb.ClockSyncRequest(host_time_us=t1_host_us)

        # [SIL-2] Bounded response timeout: do not monopolize serial flow lock for 20s.
        res = await serial.send(Command.CMD_CLOCK_SYNC.value, req, timeout=1.0)
        if isinstance(res, pb.ClockSyncResponse):
            return self.record_sync(res)
        if isinstance(res, bytes) and res:
            resp = pb.ClockSyncResponse()
            resp.ParseFromString(res)
            return self.record_sync(resp)

        if is_initial_probe:
            self.fsm.mark_unsupported()
        else:
            self.fsm.mark_degraded()
        return self.get_status()

    def record_sync(
        self, _seq_or_resp: int | pb.ClockSyncResponse, resp: pb.ClockSyncResponse | None = None
    ) -> dict[str, Any]:
        """Record clock synchronization telemetry from valid MCU response. [SIL-2]"""
        actual_resp = (
            _seq_or_resp if isinstance(_seq_or_resp, pb.ClockSyncResponse) else (resp or pb.ClockSyncResponse())
        )
        t4_host_us = time.time_ns() // 1000
        t1_host_us = actual_resp.host_time_us
        t2_mcu_us = actual_resp.mcu_time_us

        state = self._runtime.state
        if state.clock_last_host_time_us == t1_host_us and state.clock_last_host_time_us != 0:
            return self.get_status()

        rtt_us = t4_host_us - t1_host_us
        offset_us = t2_mcu_us - (t1_host_us + (rtt_us // 2))

        state.clock_offset_us = offset_us
        state.clock_rtt_us = rtt_us
        state.clock_sync_count += 1
        state.clock_last_sync_timestamp = time.time()
        state.clock_last_host_time_us = t1_host_us

        self.fsm.sync_success()

        logger.info(
            "Clock synchronization updated",
            rtt_us=rtt_us,
            offset_us=offset_us,
            mcu_time_us=t2_mcu_us,
            sync_count=state.clock_sync_count,
            fsm_state=self.fsm.current_state_value,
        )
        return self.get_status()

    def get_status(self) -> dict[str, Any]:
        """Return structured clock synchronization telemetry snapshot."""
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
        while True:
            try:
                if self._runtime.state.is_connected:
                    if not self.fsm.unsupported.is_active:
                        await self.sync_now()
                else:
                    self.fsm.disconnect()
            except asyncio.CancelledError as exc:
                logger.debug("Periodic clock sync task cancelled", error=str(exc))
                break
            except (OSError, ProtobufDecodeError) as e:
                logger.warning("Periodic clock sync failed", error=str(e))
                if self.fsm.idle.is_active or self.fsm.probing.is_active:
                    self.fsm.mark_unsupported()
                else:
                    self.fsm.mark_degraded()
            await asyncio.sleep(self._interval)
