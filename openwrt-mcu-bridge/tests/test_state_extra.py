"""Extra coverage for mcubridge.state.context."""

import asyncio
from unittest.mock import patch

import pytest
from mcubridge.config.settings import RuntimeConfig
from mcubridge.protocol.structures import CapabilitiesFeatures
from mcubridge.state.context import (
    McuCapabilities,
    ManagedProcess,
    SerialThroughputStats,
    SerialLatencyStats,
    create_runtime_state,
    _collect_system_metrics,
    _coerce_snapshot_int,
)


def test_mcu_capabilities_properties() -> None:
    feat = CapabilitiesFeatures(
        watchdog=True, rle=True, debug_frames=True, debug_io=True,
        eeprom=True, dac=True, hw_serial1=True, fpu=True,
        logic_3v3=True, large_buffer=True, i2c=True
    )
    caps = McuCapabilities(features=feat)
    assert caps.has_watchdog
    assert caps.has_rle
    assert caps.debug_frames
    assert caps.debug_io
    assert caps.has_eeprom
    assert caps.has_dac
    assert caps.has_hw_serial1
    assert caps.has_fpu
    assert caps.is_3v3_logic
    assert caps.has_large_buffer
    assert caps.has_i2c

    assert isinstance(caps.as_dict(), dict)


def test_managed_process_is_drained() -> None:
    proc = ManagedProcess(pid=1)
    assert proc.is_drained()
    proc.stdout_buffer.extend(b"data")
    assert not proc.is_drained()


def test_serial_stats_recording() -> None:
    throughput = SerialThroughputStats()
    throughput.record_tx(10)
    throughput.record_rx(20)
    assert throughput.bytes_sent == 10
    assert throughput.bytes_received == 20
    assert throughput.frames_sent == 1
    assert throughput.frames_received == 1
    assert isinstance(throughput.as_dict(), dict)


def test_serial_latency_histogram() -> None:
    latency = SerialLatencyStats()
    latency.record(1.0)   # Bucket 0 (le 5ms)
    latency.record(10000.0) # Overflow
    assert latency.bucket_counts[0] == 1
    assert latency.overflow_count == 1
    assert latency.min_latency_ms == 1.0
    assert latency.max_latency_ms == 10000.0

    # Test Prometheus init
    latency.initialize_prometheus()
    latency.record(5.0)
    assert latency.total_observations == 3


def test_collect_system_metrics_fail_paths() -> None:
    with (
        patch("psutil.cpu_percent", side_effect=OSError),
        patch("psutil.virtual_memory", side_effect=AttributeError),
        patch("psutil.getloadavg", side_effect=OSError),
    ):
        metrics = _collect_system_metrics()
        assert metrics["cpu_percent"] is None
        assert metrics["memory_total_bytes"] is None
        assert metrics["load_avg_1m"] is None


@pytest.mark.asyncio
async def test_runtime_state_supervisor_and_spool() -> None:
    config = RuntimeConfig(serial_shared_secret=b"secret_1234")
    state = create_runtime_state(config)

    # Supervisor
    state.record_supervisor_failure("task", backoff=1.0, exc=ValueError("fail"))
    assert state.supervisor_stats["task"].restarts == 1
    state.mark_supervisor_healthy("task")
    assert state.supervisor_stats["task"].backoff_seconds == 0.0

    # mark_supervisor_healthy unknown
    state.mark_supervisor_healthy("unknown")

    # Spool ensure with backoff
    state.mqtt_spool = None
    state.mqtt_spool_backoff_until = 9999999999.0
    assert await state.ensure_spool() is False


def test_coerce_snapshot_int_edge_cases() -> None:
    assert _coerce_snapshot_int({"val": "123"}, "val", 0) == 123
    assert _coerce_snapshot_int({"val": "not_int"}, "val", 42) == 42
    assert _coerce_snapshot_int({}, "val", 7) == 7


@pytest.mark.asyncio
async def test_runtime_state_mailbox_requeue_front() -> None:
    config = RuntimeConfig(serial_shared_secret=b"secret_1234")
    state = create_runtime_state(config)
    state.mailbox_queue_limit = 1
    state.requeue_mailbox_message_front(b"msg1")
    assert state.mailbox_queue_bytes > 0
    # Requeue causing overflow
    state.requeue_mailbox_message_front(b"msg2")
    assert state.mailbox_dropped_messages > 0


def test_record_serial_flow_event_unknown() -> None:
    config = RuntimeConfig(serial_shared_secret=b"secret_1234")
    state = create_runtime_state(config)
    state.record_serial_flow_event("unknown")
    assert state.serial_flow_stats.commands_sent == 0


def test_record_serial_pipeline_event_edge_cases() -> None:
    config = RuntimeConfig(serial_shared_secret=b"secret_1234")
    state = create_runtime_state(config)

    # Ack without inflight
    state.record_serial_pipeline_event({"event": "ack"})
    assert state.serial_pipeline_inflight is None

    # Success with inflight
    state.record_serial_pipeline_event({"event": "start", "command_id": 0x40})
    state.record_serial_pipeline_event({"event": "success", "command_id": 0x40})
    assert state.serial_pipeline_last["event"] == "success"


@pytest.mark.asyncio
async def test_status_writer_with_version() -> None:
    config = RuntimeConfig(serial_shared_secret=b"secret_1234")
    state = create_runtime_state(config)
    state.mcu_version = (1, 2)

    from mcubridge.state.status import status_writer
    # Run one iteration and stop via sleep patch
    with patch("mcubridge.state.status._write_status_file"), \
         patch("asyncio.sleep", side_effect=asyncio.CancelledError):
        with pytest.raises(asyncio.CancelledError):
            await status_writer(state, 1)


def test_cleanup_status_file_error() -> None:
    from mcubridge.state.status import cleanup_status_file
    with patch("mcubridge.state.status.STATUS_FILE") as mock_file:
        mock_file.unlink.side_effect = OSError
        cleanup_status_file() # Should not raise
