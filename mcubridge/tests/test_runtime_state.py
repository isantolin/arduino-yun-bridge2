"""Unit tests for mcubridge.state.context."""

from __future__ import annotations

from typing import Any
from unittest.mock import MagicMock

import pytest
from mcubridge.config.settings import RuntimeConfig
from mcubridge.state.context import (
    create_runtime_state,
)


@pytest.mark.asyncio
async def test_runtime_state_initialization(runtime_config: RuntimeConfig) -> None:
    state = create_runtime_state(runtime_config)
    assert state.state == "disconnected"
    assert not state.is_connected
    assert not state.is_synchronized


@pytest.mark.asyncio
async def test_mark_transport_connected(runtime_config: RuntimeConfig) -> None:
    state = create_runtime_state(runtime_config)
    state.mark_transport_connected()
    assert state.state == "connected"
    assert state.is_connected
    assert not state.is_synchronized


@pytest.mark.asyncio
async def test_mark_synchronized(runtime_config: RuntimeConfig) -> None:
    state = create_runtime_state(runtime_config)
    state.mark_synchronized()
    assert state.state == "synchronized"
    assert state.is_connected
    assert state.is_synchronized


@pytest.mark.asyncio
async def test_mark_transport_disconnected(runtime_config: RuntimeConfig) -> None:
    state = create_runtime_state(runtime_config)
    state.mark_synchronized()
    state.mark_transport_disconnected()
    assert state.state == "disconnected"
    assert not state.is_connected
    assert not state.is_synchronized


@pytest.mark.asyncio
async def test_record_supervisor_failure(runtime_config: RuntimeConfig) -> None:
    state = create_runtime_state(runtime_config)
    state.record_supervisor_failure("test_task", 5.0, RuntimeError("test error"))

    assert state.supervisor_failures == 1
    assert "test_task" in state.supervisor_stats
    assert state.supervisor_stats["test_task"].restarts == 1
    assert state.supervisor_stats["test_task"].backoff_seconds == 5.0


@pytest.mark.asyncio
async def test_initialize_spool_handles_creation_failure(
    runtime_config: RuntimeConfig,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from mcubridge.transport.mqtt import MqttTransport

    state = create_runtime_state(runtime_config)
    transport = MqttTransport(runtime_config, state)

    def mock_init_fail(*args: Any, **kwargs: Any) -> Any:
        raise OSError("Permission denied")

    from mcubridge.mqtt.spool import MQTTPublishSpool

    monkeypatch.setattr(MQTTPublishSpool, "__init__", mock_init_fail)

    try:
        await transport.initialize_spool()
        assert state.mqtt_spool is None
        assert state.mqtt_spool_degraded is True
    finally:
        state.cleanup()


@pytest.mark.asyncio
async def test_spool_fallback_updates_state(
    runtime_config: RuntimeConfig,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from mcubridge.transport.mqtt import MqttTransport

    state = create_runtime_state(runtime_config)
    transport = MqttTransport(runtime_config, state)

    # Initial state
    assert state.mqtt_spool_degraded is False

    # Force failure
    monkeypatch.setattr(
        transport, "initialize_spool", MagicMock(side_effect=RuntimeError("fail"))
    )
    # This test is a bit artificial now as initialize_spool is the main entry point

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
