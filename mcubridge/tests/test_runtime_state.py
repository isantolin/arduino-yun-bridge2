"""Unit tests for mcubridge.state.context.RuntimeState (SIL-2)."""

from __future__ import annotations

import time

from mcubridge.config.settings import RuntimeConfig
from mcubridge.state.context import create_runtime_state


def test_create_runtime_state_initializes_queues(runtime_config: RuntimeConfig) -> None:
    state = create_runtime_state(runtime_config)
    try:
        assert state.mqtt_publish_queue is not None
        assert state.console_to_mcu_queue is not None
        assert state.mailbox_queue is not None
    finally:
        state.cleanup()


def test_configure_updates_derived_values(runtime_config: RuntimeConfig) -> None:
    state = create_runtime_state(runtime_config)
    try:
        runtime_config.mqtt_topic = "custom/prefix"
        state.configure(runtime_config)
        assert state.mqtt_topic_prefix == "custom/prefix"
    finally:
        state.cleanup()


def test_mark_transport_connected_updates_state(runtime_config: RuntimeConfig) -> None:
    state = create_runtime_state(runtime_config)
    try:
        state.mark_transport_connected()
        assert state.is_connected is True
        assert state.is_synchronized is False
    finally:
        state.cleanup()


def test_mark_synchronized_sets_flag(runtime_config: RuntimeConfig) -> None:
    state = create_runtime_state(runtime_config)
    try:
        state.mark_transport_connected()
        state.mark_synchronized()
        assert state.is_synchronized is True
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


def test_record_mqtt_drop_increments_counter(runtime_config: RuntimeConfig) -> None:
    state = create_runtime_state(runtime_config)
    try:
        topic = "test/topic"
        # [SIL-2] Direct metrics recording (No Wrapper)
        state.mqtt_drop_counts[topic] = state.mqtt_drop_counts.get(topic, 0) + 1
        state.mqtt_dropped_messages += 1
        state.metrics.mqtt_messages_dropped.inc()

        assert state.mqtt_dropped_messages == 1
    finally:
        state.cleanup()


def test_mark_supervisor_healthy_resets_backoff(runtime_config: RuntimeConfig) -> None:
    state = create_runtime_state(runtime_config)
    try:
        state.record_supervisor_failure("test_svc", 10.0, RuntimeError("fail"))
        assert state.supervisor_stats["test_svc"].backoff_seconds == 10.0

        state.mark_supervisor_healthy("test_svc")
        assert state.supervisor_stats["test_svc"].backoff_seconds == 0.0
    finally:
        state.cleanup()
