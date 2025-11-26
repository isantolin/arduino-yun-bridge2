"""Tests for daemon connection resilience and retry logic."""
from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, MagicMock, call

import pytest
from serial import SerialException

from yunbridge.config.settings import load_runtime_config
from yunbridge.daemon import (
    _connect_mqtt_with_retry,
    _open_serial_connection_with_retry,
)
from yunbridge.mqtt import MQTTError


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
