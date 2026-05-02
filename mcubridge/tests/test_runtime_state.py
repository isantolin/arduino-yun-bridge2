"""Unit tests for mcubridge.state.context.RuntimeState (SIL-2)."""

from __future__ import annotations

from typing import Any, cast

import pytest
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


@pytest.mark.asyncio
async def test_initialize_spool_handles_creation_failure(
    runtime_config: RuntimeConfig,
    monkeypatch: pytest.MonkeyPatch,
) -> None:

    state = create_runtime_state(runtime_config)
    state.mqtt_spool_limit = 100

    def mock_init_fail(*args: Any, **kwargs: Any) -> Any:
        raise OSError("Permission denied")

    from mcubridge.mqtt.spool import MQTTPublishSpool
    from mcubridge.mqtt.queue import initialize_spool

    monkeypatch.setattr(MQTTPublishSpool, "__init__", mock_init_fail)

    initialize_spool(state)
    assert state.mqtt_spool_degraded is True
    assert state.mqtt_spool_failure_reason == "initialization_failed"


@pytest.mark.asyncio
async def test_spool_fallback_updates_state(
    runtime_config: RuntimeConfig,
) -> None:

    state = create_runtime_state(runtime_config)
    from mcubridge.mqtt.queue import _on_spool_fallback  # type: ignore[reportPrivateUsage]

    cast(Any, _on_spool_fallback)(state, "disk error")
    assert state.mqtt_spool_degraded is True
    assert state.mqtt_spool_failure_reason == "disk error"
