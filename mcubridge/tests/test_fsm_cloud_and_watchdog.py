"""[SIL-2] Deterministic state machine tests for Cloud, Watchdog, and FileTransfer."""

from __future__ import annotations

from unittest.mock import AsyncMock

from mcubridge.config.settings import RuntimeConfig
from mcubridge.protocol import mcubridge_pb2 as pb
from mcubridge.services.runtime import BridgeService
from mcubridge.state.context import (
    RuntimeState,
    create_runtime_state,
)
from mcubridge.transport.serial import SerialTransport
from mcubridge.watchdog import WatchdogKeepalive, WatchdogSupervisorMachine, WatchdogSupervisorState


def test_watchdog_supervisor_machine_lifecycle() -> None:
    """Validate all discrete states and transitions of WatchdogSupervisorMachine."""
    fsm = WatchdogSupervisorMachine()
    assert fsm.initializing.is_active

    fsm.start_healthy()
    assert fsm.healthy.is_active

    fsm.degrade()
    assert fsm.degraded.is_active

    fsm.recover()
    assert fsm.healthy.is_active

    fsm.trip_inhibit()
    assert fsm.critical_inhibit.is_active

    fsm.stop()
    assert fsm.shutdown.is_active


def test_watchdog_keepalive_fsm_integration(runtime_config: RuntimeConfig) -> None:
    """Validate WatchdogKeepalive FSM telemetry tracking."""
    state = create_runtime_state(runtime_config)
    written_bytes: list[bytes] = []

    def mock_write(b: bytes) -> None:
        written_bytes.append(b)

    watchdog = WatchdogKeepalive(
        interval=runtime_config.watchdog_interval,
        state=state,
        write=mock_write,
    )

    assert watchdog.is_healthy()
    assert watchdog.status == WatchdogSupervisorState.INITIALIZING.value
    assert watchdog.fsm.initializing.is_active

    # First kick transitions initializing -> healthy and emits pulse
    watchdog.kick()
    assert watchdog.status == WatchdogSupervisorState.HEALTHY.value
    assert watchdog.fsm.healthy.is_active
    assert len(written_bytes) == 1
    assert state.watchdog_beats == 1

    # Degrade
    watchdog.degrade("High latency on serial")
    assert watchdog.status == WatchdogSupervisorState.DEGRADED.value
    assert watchdog.fsm.degraded.is_active
    assert watchdog.is_healthy()  # Degraded still allows keepalive pulse

    # Kick while degraded recovers to healthy
    watchdog.kick()
    assert watchdog.status == WatchdogSupervisorState.HEALTHY.value
    assert watchdog.fsm.healthy.is_active
    assert len(written_bytes) == 2

    # Trip inhibit
    watchdog.trip_inhibit("Hardware fault detected")
    assert watchdog.status == WatchdogSupervisorState.CRITICAL_INHIBIT.value
    assert watchdog.fsm.critical_inhibit.is_active
    assert not watchdog.is_healthy()

    # Kick while inhibited does NOT write pulse
    watchdog.kick()
    assert len(written_bytes) == 2  # No new pulse


async def test_runtime_mcu_file_read_unary_rpc(
    service_stack: tuple[BridgeService, RuntimeState, SerialTransport],
) -> None:
    """Validate MCU file read delegates directly to unary RPC."""
    service, _, _ = service_stack

    serial_mock = AsyncMock()
    serial_mock.send.return_value = pb.FileReadResponse(content=b"part1 part2")
    service.serial = serial_mock

    resp = await service.file_read("/mcu/test.bin")
    assert resp.content == b"part1 part2"

    serial_mock.send.return_value = False
    resp_fail = await service.file_read("/mcu/missing.bin")
    assert resp_fail.content == b""
