"""Unit tests for mcubridge.state.context.RuntimeState (SIL-2)."""

from __future__ import annotations

import time

from mcubridge.config.settings import RuntimeConfig
from mcubridge.state.context import create_runtime_state


def test_create_runtime_state_initializes_queues(runtime_config: RuntimeConfig) -> None:
    state = create_runtime_state(runtime_config)
    try:
        assert state.cloud_publish_queue is not None
        assert state.console_to_mcu_queue is not None
        assert state.mailbox_queue is not None
    finally:
        state.cleanup()


def test_configure_updates_derived_values(runtime_config: RuntimeConfig) -> None:
    runtime_config.topic_prefix = "custom/prefix"
    state = create_runtime_state(runtime_config)
    try:
        assert state.topic_prefix == "custom/prefix"
    finally:
        state.cleanup()


def test_connection_fsm_connect_updates_state(runtime_config: RuntimeConfig) -> None:
    state = create_runtime_state(runtime_config)
    try:
        assert state.is_disconnected
        state.connection_fsm.connect()
        assert state.is_connected
        assert not state.is_synchronized
        assert not state.is_disconnected
    finally:
        state.cleanup()


def test_connection_fsm_synchronize_sets_flag(runtime_config: RuntimeConfig) -> None:
    state = create_runtime_state(runtime_config)
    try:
        state.connection_fsm.connect()
        state.connection_fsm.synchronize()
        assert state.is_synchronized
    finally:
        state.cleanup()


def test_record_watchdog_beat_updates_counters(runtime_config: RuntimeConfig) -> None:
    state = create_runtime_state(runtime_config)
    try:
        initial_beats = state.watchdog_beats
        # [SIL-2] Direct metrics recording (No Wrapper)
        state.watchdog_beats += 1
        state.metrics.watchdog_beats.inc()
        state.last_watchdog_beat = time.time()

        assert state.watchdog_beats == initial_beats + 1
        assert state.last_watchdog_beat > 0
    finally:
        state.cleanup()


def test_record_cloud_drop_increments_counter(runtime_config: RuntimeConfig) -> None:
    state = create_runtime_state(runtime_config)
    try:
        topic = "test/topic"
        # [SIL-2] Direct metrics recording (No Wrapper)
        state.cloud_drop_counts[topic] = state.cloud_drop_counts.get(topic, 0) + 1
        state.cloud_dropped_messages += 1
        state.metrics.cloud_messages_dropped.inc()

        assert state.cloud_dropped_messages == 1
    finally:
        state.cleanup()


def test_system_snapshot_removed(runtime_config: RuntimeConfig) -> None:
    """Verify collect_system_metrics was removed as dead code (always returned {})."""

    state = create_runtime_state(runtime_config)
    snapshot = state.build_metrics_snapshot()
    assert not hasattr(snapshot, "system")


def test_build_metrics_snapshot_includes_spool_state(runtime_config: RuntimeConfig) -> None:
    state = create_runtime_state(runtime_config)
    try:
        state.cloud_spool_degraded = True
        state.cloud_spool_failure_reason = "disk-full"
        state.cloud_spool_pending_messages = 3
        snapshot = state.build_metrics_snapshot()
        assert snapshot.cloud_spool_degraded
        assert snapshot.cloud_spool_failure_reason == "disk-full"
        assert snapshot.cloud_spool_pending_messages == 3
    finally:
        state.cleanup()


def test_link_connection_machine_lifecycle(runtime_config: RuntimeConfig) -> None:
    from mcubridge.state.context import LinkConnectionState

    state = create_runtime_state(runtime_config)
    try:
        # Initial state is disconnected
        assert state.state == LinkConnectionState.DISCONNECTED.value
        assert state.connection_fsm.current_state_value == LinkConnectionState.DISCONNECTED.value
        assert not state.is_connected
        assert not state.is_synchronized

        # Transition to connected
        state.connection_fsm.connect()
        assert state.state == LinkConnectionState.CONNECTED.value
        assert state.is_connected
        assert not state.is_synchronized
        assert state.serial_tx_allowed.is_set()

        # Idempotent connect
        state.connection_fsm.connect()
        assert state.state == LinkConnectionState.CONNECTED.value

        # Transition to synchronized
        state.connection_fsm.synchronize()
        assert state.state == LinkConnectionState.SYNCHRONIZED.value
        assert state.is_connected
        assert state.is_synchronized
        assert state.link_sync_event.is_set()

        # Idempotent synchronize
        state.connection_fsm.synchronize()
        assert state.state == LinkConnectionState.SYNCHRONIZED.value

        # Re-sync back to connected
        state.connection_fsm.connect()
        assert state.state == LinkConnectionState.CONNECTED.value
        assert state.is_connected
        assert not state.is_synchronized

        # Disconnect
        state.connection_fsm.disconnect()
        assert state.state == LinkConnectionState.DISCONNECTED.value
        assert not state.is_connected
        assert not state.is_synchronized
        assert not state.link_sync_event.is_set()

        # Idempotent disconnect
        state.connection_fsm.disconnect()
        assert state.state == LinkConnectionState.DISCONNECTED.value
    finally:
        state.cleanup()


def test_process_machine_lifecycle() -> None:
    from unittest.mock import MagicMock
    from mcubridge.state.context import ProcessContext, ProcessState, ProcessMachine

    m = ProcessMachine()
    assert m.current_state_value == ProcessState.SPAWNING.value
    m.start()
    assert m.current_state_value == ProcessState.RUNNING.value
    m.terminate()
    assert m.current_state_value == ProcessState.TERMINATING.value
    m.finish()
    assert m.current_state_value == ProcessState.EXITED.value
    assert m.is_terminated

    # Test via ProcessContext
    mock_proc = MagicMock()
    ctx = ProcessContext(mock_proc)
    assert ctx.status == ProcessState.RUNNING.value
    assert ctx.fsm.current_state_value == ProcessState.RUNNING.value
    assert ctx.is_running
    assert not ctx.is_terminating
    assert not ctx.is_exited
    ctx.fsm.terminate()
    assert ctx.status == ProcessState.TERMINATING.value
    assert ctx.is_terminating
    assert not ctx.is_running
    assert not ctx.is_exited
    ctx.fsm.finish()
    assert ctx.status == ProcessState.EXITED.value
    assert ctx.is_exited
    assert not ctx.is_running
    assert not ctx.is_terminating
    assert ctx.fsm.is_terminated
