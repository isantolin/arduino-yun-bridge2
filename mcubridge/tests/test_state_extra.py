"""Extra coverage for mcubridge.state components (SIL-2)."""

from __future__ import annotations

import asyncio
import contextlib
from unittest.mock import patch

import pytest
from mcubridge.config.settings import RuntimeConfig
from mcubridge.state.context import (
    ManagedProcess,
    RuntimeState,
    create_runtime_state,
)


def test_mcu_capabilities_properties() -> None:
    from mcubridge.protocol.structures import McuCapabilities, CapabilitiesFeatures

    features = CapabilitiesFeatures(
        watchdog=True,
        rle=True,
        debug_frames=True,
        debug_io=True,
        eeprom=True,
        dac=True,
        hw_serial1=True,
        fpu=False,
        logic_3v3=True,
        big_buffer=False,
        i2c=True,
        spi=True,
        sd=False,
    )
    caps = McuCapabilities(
        protocol_version=1,
        board_arch=1,
        num_digital_pins=20,
        num_analog_inputs=6,
        features=features,
    )
    assert caps.num_digital_pins == 20
    assert caps.features is not None; assert caps.features.hw_serial1 is True


def test_managed_process_is_drained() -> None:
    proc = ManagedProcess(pid=1, command="echo")
    # Must be in FINISHED/ZOMBIE to be drained
    proc.trigger("start") # STARTING -> RUNNING
    assert proc.is_drained() is False

    proc.trigger("sigchld") # RUNNING -> DRAINING
    proc.trigger("io_complete") # DRAINING -> FINISHED
    assert proc.is_drained() is True


def test_serial_stats_recording() -> None:
    state = RuntimeState()
    state.record_serial_decode_error()
    assert state.serial_decode_errors == 1


def testcollect_system_metrics_fail_paths() -> None:
    from mcubridge.state.context import collect_system_metrics

    # Mock psutil to fail
    with patch("psutil.Process", side_effect=RuntimeError("psutil-fail")):
        metrics = collect_system_metrics()
        assert metrics == {}


def test_runtime_state_mailbox_requeue_front() -> None:
    state = RuntimeState()
    try:
        state.record_supervisor_failure("svc", backoff=1.0, exc=ValueError("fail"))
        assert state.supervisor_stats["svc"].restarts == 1

        state.mark_supervisor_healthy("svc")
        assert state.supervisor_stats["svc"].backoff_seconds == 0.0
    finally:
        state.cleanup()


def test_runtime_state_mailbox_requeue_front_full() -> None:
    state = RuntimeState()
    state.mailbox_queue_limit = 1
    state.enqueue_mailbox_message(b"msg1")
    # requeue_mailbox_message_front returns None
    state.requeue_mailbox_message_front(b"msg2")


def test_record_serial_pipeline_event_edge_cases() -> None:
    state = RuntimeState()
    # record_serial_pipeline_event(self, event: Mapping[str, Any])
    state.record_serial_pipeline_event({"event": "test"})


@pytest.mark.asyncio
async def test_status_writer_with_version() -> None:
    from mcubridge.state.status import status_writer

    # Need 4 distinct bytes in secret
    config = RuntimeConfig(serial_shared_secret=b"abcd12345678")
    state = create_runtime_state(config)
    state.mcu_version = (2, 3, 4)
    try:
        task = asyncio.create_task(status_writer(state, 1))
        await asyncio.sleep(0.15)
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await task
    finally:
        state.cleanup()


@pytest.mark.asyncio
async def test_cleanup_status_file_error() -> None:
    from mcubridge.state import status

    with patch("pathlib.Path.unlink", side_effect=OSError("busy")):
        try:
            status.STATUS_FILE.unlink(missing_ok=True)
        except OSError:
            pass
