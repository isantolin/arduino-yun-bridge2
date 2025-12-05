"""Tests for daemon connection resilience and retry logic."""
from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, MagicMock

import pytest
from serial import SerialException

from yunbridge.config.settings import load_runtime_config
from yunbridge.daemon import (
    _RetryPolicy,
    _connect_mqtt_with_retry,
    _open_serial_connection_with_retry,
    _run_with_retry,
    _supervise_task,
)
from yunbridge.mqtt import MQTTError
from yunbridge.state.context import RuntimeState


@pytest.mark.asyncio
async def test_open_serial_connection_retries_on_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Verify serial connection retries on SerialException."""
    config = load_runtime_config()
    mock_open = AsyncMock(
        side_effect=[
            SerialException("fail 1"),
            SerialException("fail 2"),
            (MagicMock(), MagicMock()),  # Success
        ]
    )
    monkeypatch.setattr("yunbridge.daemon.OPEN_SERIAL_CONNECTION", mock_open)

    # Patch sleep to avoid waiting during tests
    mock_sleep = AsyncMock()
    monkeypatch.setattr("asyncio.sleep", mock_sleep)

    reader, writer = await _open_serial_connection_with_retry(config)

    assert mock_open.call_count == 3
    assert mock_sleep.call_count == 2
    assert isinstance(reader, MagicMock)
    assert isinstance(writer, MagicMock)


@pytest.mark.asyncio
async def test_connect_mqtt_retries_on_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Verify MQTT connection retries on MQTTError."""
    config = load_runtime_config()
    mock_client = AsyncMock()
    mock_client.connect = AsyncMock(
        side_effect=[
            MQTTError("fail 1"),
            MQTTError("fail 2"),
            None,  # Success
        ]
    )

    # Patch sleep to avoid waiting during tests
    mock_sleep = AsyncMock()
    monkeypatch.setattr("asyncio.sleep", mock_sleep)

    await _connect_mqtt_with_retry(config, mock_client)

    assert mock_client.connect.call_count == 3
    assert mock_sleep.call_count == 2


@pytest.mark.asyncio
async def test_supervisor_limits_restarts(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    state = RuntimeState()
    attempts = 0
    sleep_calls: list[float] = []

    async def failing_task() -> None:
        nonlocal attempts
        attempts += 1
        raise RuntimeError(f"boom-{attempts}")

    async def fast_sleep(delay: float) -> None:
        sleep_calls.append(delay)

    monkeypatch.setattr("yunbridge.daemon.asyncio.sleep", fast_sleep)

    with pytest.raises(RuntimeError, match="boom-3"):
        await asyncio.wait_for(
            _supervise_task(
                "unit-test",
                failing_task,
                state=state,
                max_restarts=2,
                restart_interval=5.0,
                min_backoff=0.1,
                max_backoff=0.2,
            ),
            timeout=1,
        )

    stats = state.supervisor_stats["unit-test"]
    assert stats.restarts == 3
    assert stats.fatal is True
    assert len(sleep_calls) == 2
    assert sleep_calls[0] == pytest.approx(0.1)
    assert sleep_calls[1] == pytest.approx(0.2)


@pytest.mark.asyncio
async def test_supervisor_marks_recovery(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    state = RuntimeState()
    attempts = 0

    async def flaky_task() -> None:
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            raise RuntimeError("boom")

    async def fast_sleep(_: float) -> None:
        return None

    monkeypatch.setattr("yunbridge.daemon.asyncio.sleep", fast_sleep)

    await asyncio.wait_for(
        _supervise_task(
            "unit-test",
            flaky_task,
            state=state,
            max_restarts=5,
            restart_interval=5.0,
            min_backoff=0.1,
            max_backoff=0.1,
        ),
        timeout=1,
    )

    stats = state.supervisor_stats["unit-test"]
    assert stats.restarts == 1
    assert stats.backoff_seconds == 0.0
    assert stats.fatal is False


@pytest.mark.asyncio
async def test_run_with_retry_retries_until_success(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    attempts = 0
    sleeps: list[float] = []

    async def handler() -> str:
        nonlocal attempts
        attempts += 1
        if attempts < 3:
            raise MQTTError(f"boom-{attempts}")
        return "ok"

    async def fake_sleep(delay: float) -> None:
        sleeps.append(delay)

    monkeypatch.setattr("yunbridge.daemon.asyncio.sleep", fake_sleep)

    policy = _RetryPolicy(
        action="unit-test",
        retry_exceptions=(MQTTError,),
        base_delay=0.1,
        max_delay=0.3,
    )

    result = await _run_with_retry(policy, handler)

    assert result == "ok"
    assert attempts == 3
    assert sleeps == [pytest.approx(0.1), pytest.approx(0.2)]


@pytest.mark.asyncio
async def test_run_with_retry_honors_announce_callback(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    attempts = 0
    announces = 0
    sleeps: list[float] = []

    async def handler() -> None:
        nonlocal attempts
        attempts += 1
        if attempts <= 2:
            raise MQTTError("boom")

    async def fake_sleep(delay: float) -> None:
        sleeps.append(delay)

    def announce() -> None:
        nonlocal announces
        announces += 1

    monkeypatch.setattr("yunbridge.daemon.asyncio.sleep", fake_sleep)

    policy = _RetryPolicy(
        action="unit-test",
        retry_exceptions=(MQTTError,),
        base_delay=0.05,
        max_delay=0.2,
        announce_attempt=announce,
    )

    await _run_with_retry(policy, handler)

    assert attempts == 3
    assert announces == 3
    assert len(sleeps) == 2


@pytest.mark.asyncio
async def test_run_with_retry_handles_exception_group(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    attempts = 0
    sleeps: list[float] = []

    async def handler() -> str:
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            raise ExceptionGroup("group", [MQTTError("boom")])
        return "ok"

    async def fake_sleep(delay: float) -> None:
        sleeps.append(delay)

    monkeypatch.setattr("yunbridge.daemon.asyncio.sleep", fake_sleep)

    policy = _RetryPolicy(
        action="unit-test",
        retry_exceptions=(MQTTError,),
        base_delay=0.1,
        max_delay=0.2,
    )

    result = await _run_with_retry(policy, handler)

    assert result == "ok"
    assert attempts == 2
    assert len(sleeps) == 1


@pytest.mark.asyncio
async def test_run_with_retry_rejects_mixed_exception_group(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def handler() -> None:
        raise ExceptionGroup(
            "mixed",
            [MQTTError("retryable"), ValueError("fatal")],
        )

    async def fake_sleep(_: float) -> None:
        raise AssertionError("sleep should not be invoked")

    monkeypatch.setattr("yunbridge.daemon.asyncio.sleep", fake_sleep)

    policy = _RetryPolicy(
        action="unit-test",
        retry_exceptions=(MQTTError,),
        base_delay=0.1,
        max_delay=0.2,
    )

    with pytest.raises(ExceptionGroup, match="mixed"):
        await _run_with_retry(policy, handler)


@pytest.mark.asyncio
async def test_run_with_retry_bubbles_non_retry_exception(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    sleeps: list[float] = []

    async def handler() -> None:
        raise ValueError("no retry")

    async def fake_sleep(delay: float) -> None:
        sleeps.append(delay)

    monkeypatch.setattr("yunbridge.daemon.asyncio.sleep", fake_sleep)

    policy = _RetryPolicy(
        action="unit-test",
        retry_exceptions=(MQTTError,),
        base_delay=0.1,
        max_delay=0.2,
    )

    with pytest.raises(ValueError):
        await _run_with_retry(policy, handler)

    assert sleeps == []
