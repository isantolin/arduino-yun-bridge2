"""Unit tests for mcubridge.state.context.RuntimeState (SIL-2)."""

from __future__ import annotations

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
    from mcubridge.watchdog import WatchdogKeepalive

    state = create_runtime_state(runtime_config)
    try:
        initial_beats = state.watchdog_beats
        watchdog = WatchdogKeepalive(interval=1.0, state=state, write=lambda _: None)
        watchdog.kick()

        assert state.watchdog_beats == initial_beats + 1
        assert state.last_watchdog_beat > 0
    finally:
        state.cleanup()


def test_record_cloud_drop_increments_counter(runtime_config: RuntimeConfig) -> None:
    from unittest.mock import AsyncMock

    from mcubridge.services.runtime import BridgeService

    state = create_runtime_state(runtime_config)
    try:
        service = BridgeService(runtime_config, state, AsyncMock())
        topic = "test/topic"
        record_drop = getattr(service, "_record_cloud_drop")
        record_drop(topic)

        assert state.cloud_dropped_messages == 1
        assert state.cloud_drop_counts[topic] == 1
    finally:
        state.cleanup()


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
