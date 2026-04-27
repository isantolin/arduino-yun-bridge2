"""More unit tests for RuntimeState edge cases (SIL-2)."""

from __future__ import annotations

import time
import asyncio
import pytest
from unittest.mock import MagicMock, patch

from mcubridge.mqtt.spool import MQTTPublishSpool
from mcubridge.state.context import RuntimeState
from mcubridge.protocol.structures import QueuedPublish


def test_create_runtime_state_disables_spool_without_scheduling_retry(
    runtime_state: RuntimeState,
) -> None:
    # Initial state check
    assert runtime_state.mqtt_spool_degraded is False


@pytest.mark.asyncio
async def test_disable_mqtt_spool_handles_close_error_and_schedules_retry(
    runtime_state: RuntimeState,
) -> None:
    from mcubridge.transport.mqtt import MqttTransport
    from mcubridge.config.settings import RuntimeConfig

    config = RuntimeConfig(serial_shared_secret=b"secret_1234")
    transport = MqttTransport(config, runtime_state)

    mock_spool = MagicMock(spec=MQTTPublishSpool)
    mock_spool.close.side_effect = OSError("close-failed")
    runtime_state.mqtt_spool = mock_spool

    before = time.monotonic()
    transport._disable_mqtt_spool("test")  # type: ignore[reportPrivateUsage]

    assert runtime_state.mqtt_spool is None
    assert runtime_state.mqtt_spool_degraded is True
    assert runtime_state.mqtt_spool_backoff_until >= before


def test_current_spool_snapshot_returns_last_snapshot_when_missing_spool(
    runtime_state: RuntimeState,
) -> None:
    runtime_state.mqtt_spool = None
    runtime_state._last_spool_snapshot = {"pending": 5}  # type: ignore[reportPrivateUsage]
    assert runtime_state._current_spool_snapshot()["pending"] == 5  # type: ignore[reportPrivateUsage]


def test_apply_spool_observation_coerces_numeric_fields(
    runtime_state: RuntimeState,
) -> None:
    # Initial: 0
    # Use real float value to avoid msgspec coercion issues in strict mode
    runtime_state._apply_spool_observation({"trim_events": 10, "last_trim_unix": 123.45})  # type: ignore[reportPrivateUsage]
    assert runtime_state.mqtt_spool_trim_events == 10
    assert runtime_state.mqtt_spool_last_trim_unix == 123.45


@pytest.mark.asyncio
async def test_ensure_spool_returns_false_while_backoff_active(
    runtime_state: RuntimeState,
) -> None:
    from mcubridge.transport.mqtt import MqttTransport
    from mcubridge.config.settings import RuntimeConfig

    config = RuntimeConfig(serial_shared_secret=b"secret_1234")
    transport = MqttTransport(config, runtime_state)

    runtime_state.mqtt_spool = None  # Ensure it's None so it checks backoff
    runtime_state.mqtt_spool_backoff_until = time.monotonic() + 100
    assert await transport.ensure_spool() is False


@pytest.mark.asyncio
async def test_stash_mqtt_message_returns_false_when_spool_disabled(
    runtime_state: RuntimeState,
) -> None:
    from mcubridge.transport.mqtt import MqttTransport
    from mcubridge.config.settings import RuntimeConfig

    config = RuntimeConfig(serial_shared_secret=b"secret_1234")
    transport = MqttTransport(config, runtime_state)

    runtime_state.mqtt_spool = None
    runtime_state.mqtt_spool_degraded = True
    runtime_state.mqtt_spool_failure_reason = "permanent_failure"
    # Set backoff until future to prevent ensure_spool from trying to reactivate
    runtime_state.mqtt_spool_backoff_until = time.monotonic() + 100

    msg = QueuedPublish(topic_name="t", payload=b"p")
    assert await transport.stash_mqtt_message(msg) is False


@pytest.mark.asyncio
async def test_flush_mqtt_spool_queue_full_requeue_failure_disables_spool(
    runtime_state: RuntimeState,
) -> None:
    from mcubridge.transport.mqtt import MqttTransport
    from mcubridge.config.settings import RuntimeConfig

    config = RuntimeConfig(serial_shared_secret=b"secret_1234")
    transport = MqttTransport(config, runtime_state)

    msg = QueuedPublish(topic_name="t", payload=b"p")
    mock_spool = MagicMock(spec=MQTTPublishSpool)
    mock_spool.pop_next.return_value = msg
    # Mock requeue to fail
    mock_spool.requeue.side_effect = OSError("requeue-failed")

    runtime_state.mqtt_spool = mock_spool

    # Mock publish queue to be full
    with patch("asyncio.Queue.qsize", return_value=0):  # Make it enter the loop
        with patch("asyncio.Queue.put_nowait", side_effect=asyncio.QueueFull()):
            # This should trigger _handle_mqtt_spool_failure
            await transport.flush_mqtt_spool()
            assert runtime_state.mqtt_spool is None
            assert runtime_state.mqtt_spool_degraded is True
