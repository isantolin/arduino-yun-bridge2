"""[SIL-2] Deterministic state machine tests for Cloud, Watchdog, and FileTransfer."""

from __future__ import annotations

import asyncio
from typing import Any
from unittest.mock import AsyncMock

import pytest
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


@pytest.mark.asyncio
async def test_runtime_mcu_file_read_fsm_success(
    service_stack: tuple[BridgeService, RuntimeState, SerialTransport],
) -> None:
    """Validate MCU file read handler driving FileTransferMachine on success."""
    service, _, _ = service_stack

    serial_mock = AsyncMock()
    serial_mock.send_raw.return_value = True
    setattr(service, "serial", serial_mock)

    ctx = pb.CloudQueuedPublish(topic_name="mcu/fs/read", payload=b"")

    async def _simulate_mcu_responses() -> None:
        while getattr(service, "_pending_mcu_read") is None:
            await asyncio.sleep(0.01)

        pending: Any = getattr(service, "_pending_mcu_read")
        assert pending is not None
        assert pending.fsm.transferring.is_active

        on_resp_fn: Any = getattr(service, "_on_mcu_file_read_resp")
        # Send chunk 1
        await on_resp_fn(1, pb.FileReadResponse(content=b"part1 "))
        assert pending.fsm.transferring.is_active

        # Send chunk 2
        await on_resp_fn(2, pb.FileReadResponse(content=b"part2"))
        assert pending.fsm.transferring.is_active

        # Send EOF
        await on_resp_fn(3, pb.FileReadResponse(content=b""))
        assert pending.fsm.completed.is_active

    handle_fn: Any = getattr(service, "_handle_file_mcu_read")
    async with asyncio.TaskGroup() as tg:
        tg.create_task(_simulate_mcu_responses())
        tg.create_task(handle_fn("/mcu/test.bin", ctx))

    assert getattr(service, "_pending_mcu_read") is None


@pytest.mark.asyncio
async def test_runtime_mcu_file_read_fsm_dispatch_fail(
    service_stack: tuple[BridgeService, RuntimeState, SerialTransport],
) -> None:
    """Validate FileTransferMachine abort on dispatch failure."""
    service, _, _ = service_stack

    serial_mock = AsyncMock()
    serial_mock.send_raw.return_value = False
    setattr(service, "serial", serial_mock)

    ctx = pb.CloudQueuedPublish(topic_name="mcu/fs/read", payload=b"")
    handle_fn: Any = getattr(service, "_handle_file_mcu_read")
    await handle_fn("/mcu/missing.bin", ctx)

    assert getattr(service, "_pending_mcu_read") is None


@pytest.mark.asyncio
async def test_runtime_mcu_file_read_fsm_timeout(
    service_stack: tuple[BridgeService, RuntimeState, SerialTransport],
) -> None:
    """Validate FileTransferMachine timeout on MCU unresponsive."""
    service, state, _ = service_stack
    state.serial_response_timeout_ms = 50

    serial_mock = AsyncMock()
    serial_mock.send_raw.return_value = True
    setattr(service, "serial", serial_mock)

    ctx = pb.CloudQueuedPublish(topic_name="mcu/fs/read", payload=b"")
    handle_fn: Any = getattr(service, "_handle_file_mcu_read")
    await handle_fn("/mcu/unresponsive.bin", ctx)

    assert getattr(service, "_pending_mcu_read") is None
