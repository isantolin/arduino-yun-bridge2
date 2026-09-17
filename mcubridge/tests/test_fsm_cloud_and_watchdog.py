"""[SIL-2] Deterministic state machine tests for Cloud, Watchdog, and FileTransfer."""

from __future__ import annotations

import asyncio
from typing import Any
from unittest.mock import AsyncMock

import pytest

from mcubridge.config.settings import RuntimeConfig
from mcubridge.protocol import mcubridge_pb2 as pb
import mcubridge.services.runtime as runtime_mod
from mcubridge.services.runtime import BridgeService
from mcubridge.state.context import (
    CloudLinkMachine,
    CloudLinkState,
    FileTransferMachine,
    RuntimeState,
    create_runtime_state,
)
from mcubridge.transport.serial import SerialTransport
from mcubridge.watchdog import WatchdogKeepalive, WatchdogSupervisorMachine, WatchdogSupervisorState


def test_cloud_link_machine_standalone() -> None:
    """Validate CloudLinkMachine transitions directly without model binding."""
    fsm = CloudLinkMachine()
    assert fsm.disabled.is_active

    fsm.start_connecting()
    assert fsm.connecting.is_active

    fsm.connect_http3()
    assert fsm.connected_http3.is_active

    fsm.degrade()
    assert fsm.spooling_degraded.is_active

    fsm.start_reconnect()
    assert fsm.reconnecting.is_active

    fsm.connect_http2()
    assert fsm.connected_http2.is_active

    fsm.disable()
    assert fsm.disabled.is_active


def test_pending_mcu_read_dataclass_initialization() -> None:
    """Validate _PendingMcuRead dataclass fields and FSM defaults."""
    pending_cls: Any = getattr(runtime_mod, "_PendingMcuRead")
    loop = asyncio.new_event_loop()
    try:
        fut = loop.create_future()
        pending: Any = pending_cls(future=fut)
        assert pending.future is fut
        assert pending.chunks == []
        assert pending.fsm.idle.is_active
    finally:
        loop.close()


def test_cloud_link_machine_lifecycle() -> None:
    """Validate all discrete states and transitions of CloudLinkMachine."""
    state = RuntimeState()
    fsm = state.cloud_fsm

    assert state.cloud_link_state == CloudLinkState.DISABLED.value
    assert not state.is_cloud_connected

    # Disabled -> Connecting
    fsm.start_connecting()
    assert state.cloud_link_state == CloudLinkState.CONNECTING.value
    assert not state.is_cloud_connected

    # Connecting -> Connected HTTP/3
    fsm.connect_http3()
    assert state.cloud_link_state == CloudLinkState.CONNECTED_HTTP3.value
    assert state.connected_via_http3
    assert state.is_cloud_connected

    # Connected HTTP/3 -> Spooling Degraded
    fsm.degrade()
    assert state.cloud_link_state == CloudLinkState.SPOOLING_DEGRADED.value
    assert not state.is_cloud_connected

    # Spooling Degraded -> Reconnecting
    fsm.start_reconnect()
    assert state.cloud_link_state == CloudLinkState.RECONNECTING.value
    assert not state.is_cloud_connected

    # Reconnecting -> Connected HTTP/2
    fsm.connect_http2()
    assert state.cloud_link_state == CloudLinkState.CONNECTED_HTTP2.value
    assert not state.connected_via_http3
    assert state.is_cloud_connected

    # Connected HTTP/2 -> Disabled
    fsm.disable()
    assert state.cloud_link_state == CloudLinkState.DISABLED.value
    assert not state.is_cloud_connected


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


def test_file_transfer_machine_lifecycle() -> None:
    """Validate FileTransferMachine states and streaming events."""
    fsm = FileTransferMachine()
    assert fsm.idle.is_active

    # Happy path: idle -> transferring -> completed -> idle
    fsm.start_transfer()
    assert fsm.transferring.is_active

    fsm.receive_chunk()
    assert fsm.transferring.is_active

    fsm.complete()
    assert fsm.completed.is_active

    fsm.reset()
    assert fsm.idle.is_active

    # Timeout path: idle -> transferring -> timed_out -> idle
    fsm.start_transfer()
    fsm.timeout()
    assert fsm.timed_out.is_active
    fsm.reset()
    assert fsm.idle.is_active

    # Abort path: idle -> transferring -> aborted -> idle
    fsm.start_transfer()
    fsm.abort()
    assert fsm.aborted.is_active
    fsm.reset()
    assert fsm.idle.is_active


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
        tg.create_task(handle_fn(ctx, "/mcu/test.bin"))

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
    await handle_fn(ctx, "/mcu/missing.bin")

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
    await handle_fn(ctx, "/mcu/unresponsive.bin")

    assert getattr(service, "_pending_mcu_read") is None
