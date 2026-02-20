"""Additional unit tests for RuntimeState coverage gaps."""

from __future__ import annotations

import asyncio
import time
from typing import cast

import pytest
from mcubridge.config.settings import RuntimeConfig
from mcubridge.mqtt.messages import QueuedPublish
from mcubridge.mqtt.spool import MQTTPublishSpool, MQTTSpoolError
from mcubridge.protocol import protocol
from mcubridge.state.context import RuntimeState, create_runtime_state


def test_create_runtime_state_disables_spool_without_scheduling_retry(
    runtime_config: RuntimeConfig,
) -> None:
    runtime_config.mqtt_spool_dir = ""

    state = create_runtime_state(runtime_config)

    assert state.mqtt_spool is None
    assert state.mqtt_spool_degraded is True
    assert state.mqtt_spool_failure_reason == "disabled"
    assert state.mqtt_spool_retry_attempts == 0
    assert state.mqtt_spool_backoff_until == 0.0


def test_disable_mqtt_spool_handles_close_error_and_schedules_retry(
    runtime_state: RuntimeState,
) -> None:
    class _BrokenCloseSpool:
        def close(self) -> None:
            raise OSError("close-failed")

    runtime_state.mqtt_spool = cast(MQTTPublishSpool, _BrokenCloseSpool())

    before = time.monotonic()
    runtime_state._disable_mqtt_spool("test")

    assert runtime_state.mqtt_spool is None
    assert runtime_state.mqtt_spool_degraded is True
    assert runtime_state.mqtt_spool_failure_reason == "test"
    assert runtime_state.mqtt_spool_retry_attempts == 1
    assert runtime_state.mqtt_spool_backoff_until >= before


def test_current_spool_snapshot_returns_last_snapshot_when_missing_spool(
    runtime_state: RuntimeState,
) -> None:
    runtime_state.mqtt_spool = None
    runtime_state._last_spool_snapshot = {"pending": 3, "limit": 9}

    snapshot = runtime_state._current_spool_snapshot()

    assert snapshot == {"pending": 3, "limit": 9}


def test_apply_spool_observation_coerces_numeric_fields(
    runtime_state: RuntimeState,
) -> None:
    runtime_state._apply_spool_observation(
        {
            "dropped_due_to_limit": "7",
            "trim_events": 2.0,
            "corrupt_dropped": 4.0,
            "last_trim_unix": 123.4,
        }
    )

    assert runtime_state.mqtt_spool_dropped_limit == 7
    assert runtime_state.mqtt_spool_trim_events == 2
    assert runtime_state.mqtt_spool_corrupt_dropped == 4
    assert runtime_state.mqtt_spool_last_trim_unix == 123.4


def test_ensure_spool_returns_false_while_backoff_active(
    runtime_config: RuntimeConfig,
    tmp_path_factory: pytest.TempPathFactory,
) -> None:
    async def _run() -> None:
        runtime_config.mqtt_spool_dir = str(tmp_path_factory.mktemp("spool"))
        state = create_runtime_state(runtime_config)
        assert state.mqtt_spool is not None
        state.mqtt_spool.close()
        state.mqtt_spool = None

        state.mqtt_spool_backoff_until = time.monotonic() + 60.0
        recovered = await state.ensure_spool()

        assert recovered is False
        assert state.mqtt_spool is None

    asyncio.run(_run())


def test_stash_mqtt_message_returns_false_when_spool_disabled(
    runtime_config: RuntimeConfig,
) -> None:
    async def _run() -> None:
        runtime_config.mqtt_spool_dir = ""
        state = create_runtime_state(runtime_config)

        stored = await state.stash_mqtt_message(
            QueuedPublish(
                topic_name=f"{protocol.MQTT_DEFAULT_TOPIC_PREFIX}/test",
                payload=b"{}",
            )
        )

        assert stored is False

    asyncio.run(_run())


def test_flush_mqtt_spool_queue_full_requeue_failure_disables_spool(
    runtime_config: RuntimeConfig,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def _run() -> None:
        state = create_runtime_state(runtime_config)
        if state.mqtt_spool is not None:
            state.mqtt_spool.close()

        state.mqtt_queue_limit = 10
        state.mqtt_publish_queue = asyncio.Queue(maxsize=1)
        state.mqtt_publish_queue.put_nowait(
            QueuedPublish(
                topic_name=f"{protocol.MQTT_DEFAULT_TOPIC_PREFIX}/full",
                payload=b"1",
            )
        )

        message = QueuedPublish(
            topic_name=f"{protocol.MQTT_DEFAULT_TOPIC_PREFIX}/test",
            payload=b"2",
        )

        class _StubSpool:
            def pop_next(self) -> QueuedPublish | None:
                return message

            def requeue(self, _message: QueuedPublish) -> None:
                raise MQTTSpoolError("requeue_failed")

            def close(self) -> None:
                return None

        state.mqtt_spool = cast(MQTTPublishSpool, _StubSpool())

        async def _inline_to_thread(fn, /, *args, **kwargs):
            return fn(*args, **kwargs)

        monkeypatch.setattr(asyncio, "to_thread", _inline_to_thread)

        await state.flush_mqtt_spool()

        assert state.mqtt_spool is None
        assert state.mqtt_spool_degraded is True
        assert state.mqtt_spool_failure_reason in {"requeue_failed", "requeue_failed:requeue_failed"}
        assert state.mqtt_spool_errors >= 1

    asyncio.run(_run())
