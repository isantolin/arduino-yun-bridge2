"""Tests for serial transport resiliency."""

import asyncio
from unittest.mock import MagicMock, AsyncMock, patch
import pytest
import serial

from yunbridge.config.settings import RuntimeConfig
from yunbridge.transport.serial import (
    _open_serial_connection_with_retry,
    serial_reader_task,
)


@pytest.mark.asyncio
async def test_open_serial_connection_retry_success():
    """Test that connection eventually succeeds after retries."""
    config = RuntimeConfig(
        serial_port="/dev/test0",
        serial_baud=115200,
        mqtt_host="localhost",
        mqtt_port=1883,
        mqtt_user=None,
        mqtt_pass=None,
        mqtt_tls=False,
        mqtt_cafile=None,
        mqtt_certfile=None,
        mqtt_keyfile=None,
        mqtt_topic="br",
        allowed_commands=(),
        file_system_root="/tmp",
        process_timeout=5,
        reconnect_delay=1,  # Fast retry
        serial_shared_secret=b"unit-test-secret-1234",
    )

    # Mock OPEN_SERIAL_CONNECTION to fail twice then succeed
    mock_connect = AsyncMock()
    mock_connect.side_effect = [
        serial.SerialException("Fail 1"),
        OSError("Fail 2"),
        (AsyncMock(), AsyncMock()),  # Success
    ]

    with (
        patch("yunbridge.transport.serial.OPEN_SERIAL_CONNECTION", mock_connect),
        patch(
            "yunbridge.transport.serial.asyncio.sleep", new_callable=AsyncMock
        ) as mock_sleep,
    ):
        reader, writer = await _open_serial_connection_with_retry(config)

        assert mock_connect.call_count == 3
        assert reader is not None
        assert writer is not None
        assert mock_sleep.call_count == 2


@pytest.mark.asyncio
async def test_open_serial_connection_cancelled():
    """Test that retry loop respects cancellation."""
    config = RuntimeConfig(
        serial_port="/dev/test0",
        serial_baud=115200,
        mqtt_host="localhost",
        mqtt_port=1883,
        mqtt_user=None,
        mqtt_pass=None,
        mqtt_tls=False,
        mqtt_cafile=None,
        mqtt_certfile=None,
        mqtt_keyfile=None,
        mqtt_topic="br",
        allowed_commands=(),
        file_system_root="/tmp",
        process_timeout=5,
        reconnect_delay=1,
        serial_shared_secret=b"unit-test-secret-1234",
    )

    mock_connect = AsyncMock()
    mock_connect.side_effect = serial.SerialException("Permanent Fail")

    with patch("yunbridge.transport.serial.OPEN_SERIAL_CONNECTION", mock_connect):
        # Create a task that we will cancel
        task = asyncio.create_task(_open_serial_connection_with_retry(config))

        # Allow retry loop to hit the first exception and sleep
        # Since we mocked sleep, we need to yield control to let the task run
        for _ in range(10):
            if mock_connect.call_count > 0:
                break
            await asyncio.sleep(0.1)

        task.cancel()


@pytest.mark.asyncio
async def test_serial_reader_task_reconnects():
    """Test that reader task re-establishes connection on failure."""
    config = RuntimeConfig(
        serial_port="/dev/test0",
        serial_baud=115200,
        mqtt_host="localhost",
        mqtt_port=1883,
        mqtt_user=None,
        mqtt_pass=None,
        mqtt_tls=False,
        mqtt_cafile=None,
        mqtt_certfile=None,
        mqtt_keyfile=None,
        mqtt_topic="br",
        allowed_commands=(),
        file_system_root="/tmp",
        process_timeout=5,
        reconnect_delay=1,
        serial_shared_secret=b"unit-test-secret-1234",
    )
    state = MagicMock()
    service = MagicMock()
    service.on_serial_connected = AsyncMock()
    service.on_serial_disconnected = AsyncMock()

    # Mock the reader to return EOF immediately to trigger reconnect
    mock_reader = AsyncMock()
    mock_reader.read.side_effect = [
        b"",
        asyncio.CancelledError(),
    ]  # First EOF, then exit loop via cancel

    # StreamWriter has mixed sync/async methods
    mock_writer = MagicMock()
    mock_writer.drain = AsyncMock()
    mock_writer.wait_closed = AsyncMock()
    mock_writer.is_closing.return_value = False

    # Mock connect to return our stream mocks
    mock_connect = AsyncMock(return_value=(mock_reader, mock_writer))

    with patch(
        "yunbridge.transport.serial._open_serial_connection_with_retry", mock_connect
    ):
        # Run the task, it should connect, read EOF, warn, then try to connect again
        # We raise CancelledError on 2nd read attempt to break the infinite loop for test
        try:
            await serial_reader_task(config, state, service)
        except asyncio.CancelledError:
            pass

    # Verify behavior
    assert service.on_serial_connected.called
    assert service.on_serial_disconnected.called
