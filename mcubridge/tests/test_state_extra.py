from unittest.mock import patch

import pytest
from mcubridge.state.context import (
    PROCESS_STATE_FINISHED,
    ManagedProcess,
    McuCapabilities,
    RuntimeState,
    SerialLatencyStats,
    collect_system_metrics,
)
from mcubridge.state.status import cleanup_status_file


def test_mcu_capabilities_properties() -> None:
    caps = McuCapabilities()
    assert not caps.has_watchdog
    assert not caps.has_rle
    assert not caps.has_debug_frames
    assert not caps.has_debug_io
    assert not caps.has_eeprom
    assert not caps.has_dac
    assert not caps.has_hw_serial1
    assert not caps.has_fpu
    assert not caps.is_3v3_logic
    assert not caps.has_big_buffer
    assert not caps.has_i2c
    assert not caps.has_spi

    d = caps.as_dict()
    assert not d["has_watchdog"]


def test_managed_process_is_drained() -> None:
    proc = ManagedProcess(pid=1)

    # [FSM] Set state to FINISHED so is_drained returns True
    proc.fsm_state = PROCESS_STATE_FINISHED

    assert proc.is_drained()


def test_serial_stats_recording() -> None:
    state = RuntimeState()
    try:
        state.record_serial_tx(10)
        assert state.serial_throughput_stats.bytes_sent == 10
        state.record_serial_rx(20)
        assert state.serial_throughput_stats.bytes_received == 20
    finally:
        state.cleanup()


def test_serial_latency_histogram() -> None:
    stats = SerialLatencyStats()
    # Record values in different buckets
    stats.record(4.0)  # <= 5
    stats.record(12.0)  # <= 25 (actually <= 10? No buckets are 5, 10, 25...)
    # buckets: 5, 10, 25...
    # 4.0 -> le_5
    # 12.0 -> le_25
    stats.record(3000.0)  # overflow

    d = stats.as_dict()
    buckets = d["buckets"]
    assert buckets["le_5ms"] == 1
    assert buckets["le_25ms"] == 2  # Cumulative
    assert d["overflow"] == 1
    assert d["min_ms"] == 4.0
    assert d["max_ms"] == 3000.0

def testcollect_system_metrics_fail_paths() -> None:
    # Mock psutil functions individually
    with (
        patch("psutil.Process") as mock_proc,
        patch("psutil.cpu_percent", side_effect=OSError("fail")),
        patch("psutil.virtual_memory", side_effect=OSError("fail")),
        patch("psutil.getloadavg", side_effect=OSError("fail")),
        patch("psutil.sensors_temperatures", side_effect=OSError("fail")),
    ):
        # Configure the mock process to handle oneshot() context manager
        mock_instance = mock_proc.return_value
        mock_instance.oneshot.return_value.__enter__.return_value = None

        # If the mock fails, the function returns empty dict
        assert collect_system_metrics() == {}


def test_runtime_state_mailbox_requeue_front() -> None:
    state = RuntimeState()
    try:
        state.record_supervisor_failure("svc", backoff=1.0, exc=ValueError("fail"))
        assert state.supervisor_stats["svc"].restarts == 1
        assert state.supervisor_stats["svc"].last_exception

        state.mark_supervisor_healthy("svc")
        assert state.supervisor_stats["svc"].backoff_seconds == 0.0

        state.mark_supervisor_healthy("unknown")  # Should not crash

        # Spool retry logic
        state.mqtt_spool_retry_attempts = 0
        state._schedule_spool_retry()
        assert state.mqtt_spool_retry_attempts == 1
        assert state.mqtt_spool_backoff_until > 0
    finally:
        state.cleanup()


def test_runtime_state_mailbox_requeue_front_full() -> None:
    from mcubridge.state.context import create_runtime_state
    from mcubridge.config.settings import RuntimeConfig

    config = RuntimeConfig(
        mailbox_queue_limit=5,
        mailbox_queue_bytes_limit=100,
        serial_shared_secret=b"valid_secret_1234",
    )
    state = create_runtime_state(config)

    state.requeue_mailbox_message_front(b"msg1")
    assert state.mailbox_queue_bytes > 0
    assert state.mailbox_dropped_messages == 0

    # Force overflow
    state.mailbox_queue_limit = 1
    state.requeue_mailbox_message_front(b"msg2")
    assert state.mailbox_dropped_messages > 0


def test_record_serial_flow_event_unknown() -> None:
    state = RuntimeState()
    try:
        # Should ignore unknown event
        state.record_serial_flow_event("unknown")
        assert state.serial_flow_stats.commands_sent == 0
    finally:
        state.cleanup()


def test_record_serial_pipeline_event_edge_cases() -> None:
    state = RuntimeState()
    try:
        # Event without inflight
        state.record_serial_pipeline_event({"event": "ack"})
        assert state.serial_pipeline_inflight is None

        # Start event
        state.record_serial_pipeline_event({"event": "start", "command_id": 1})
        assert state.serial_pipeline_inflight["command_id"] == 1

        # Success event with existing inflight
        state.record_serial_pipeline_event({"event": "success", "command_id": 1})
        assert state.serial_pipeline_last["status_name"] == "unknown"
    finally:
        state.cleanup()


@pytest.mark.asyncio
async def test_status_writer_with_version() -> None:
    # Test _write_status_file directly instead of the infinite loop
    with patch("mcubridge.state.status.NamedTemporaryFile") as mock_tf:
        with patch("mcubridge.state.status.Path"):
            from mcubridge.state.status import _write_status_file

            # Mock build_metrics_snapshot on the CLASS because msgspec.Struct instances are rigid
            with patch(
                "mcubridge.state.context.RuntimeState.build_metrics_snapshot",
                return_value={"test": 1},
            ):
                state = RuntimeState()
                try:
                    state.mcu_version = (1, 2)
                    state.mcu_capabilities = McuCapabilities()
                    _write_status_file(state.build_metrics_snapshot())
                    assert mock_tf.called
                finally:
                    state.cleanup()


def test_cleanup_status_file_error() -> None:
    with patch("mcubridge.state.status.STATUS_FILE") as mock_path:
        mock_path.unlink.side_effect = OSError("fail")
        cleanup_status_file()  # Should not raise
