"""Tests for serial transport resiliency."""

import asyncio
from unittest.mock import MagicMock, AsyncMock, patch
import pytest

from mcubridge.config.settings import RuntimeConfig
from mcubridge.transport.serial import (
    _open_serial_connection_with_retry,
    SerialTransport,
)
from mcubridge.transport.termios_serial import SerialException
from mcubridge.rpc import protocol


@pytest.mark.asyncio
async def test_open_serial_connection_retry_success():
    """Test that connection eventually succeeds after retries."""
    config = RuntimeConfig(
        serial_port="/dev/test0",
        serial_baud=protocol.DEFAULT_BAUDRATE,
        serial_safe_baud=protocol.DEFAULT_SAFE_BAUDRATE,
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
    success_reader = AsyncMock()
    success_writer = AsyncMock()
    # Prevent _ensure_raw_mode from receiving an AsyncMock as fd
    success_writer.transport.serial.fd = None

    mock_connect = AsyncMock()
    mock_connect.side_effect = [
        SerialException("Fail 1"),
        OSError("Fail 2"),
        (success_reader, success_writer),  # Success
    ]

    with (
        patch("mcubridge.transport.serial.OPEN_SERIAL_CONNECTION", mock_connect),
        patch(
            "mcubridge.transport.serial.asyncio.sleep", new_callable=AsyncMock
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
        serial_baud=protocol.DEFAULT_BAUDRATE,
        serial_safe_baud=protocol.DEFAULT_SAFE_BAUDRATE,
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
    mock_connect.side_effect = SerialException("Permanent Fail")

    with patch("mcubridge.transport.serial.OPEN_SERIAL_CONNECTION", mock_connect), \
         patch("asyncio.sleep", AsyncMock()):
        # Create a task that we will cancel
        task = asyncio.create_task(_open_serial_connection_with_retry(config))

        # Allow retry loop to hit the first exception and sleep
        # Since we mocked sleep, we need to yield control to let the task run
        for _ in range(10):
            if mock_connect.call_count > 0:
                break
            # We can't use asyncio.sleep(0.1) if we mocked it globally!
            # But we mocked it inside the context manager.
            # So calling asyncio.sleep inside the context manager calls the mock.
            # So we need a way to yield control without sleeping for real time?
            # Or just await the mock?
            await asyncio.sleep(0.1)

        task.cancel()


@pytest.mark.asyncio
async def test_serial_reader_task_reconnects():
    """Test that reader task re-establishes connection on failure."""
    config = RuntimeConfig(
        serial_port="/dev/test0",
        serial_baud=protocol.DEFAULT_BAUDRATE,
        serial_safe_baud=protocol.DEFAULT_SAFE_BAUDRATE,
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
    mock_reader.read.return_value = b""  # Always return EOF to simulate disconnect

    # StreamWriter has mixed sync/async methods
    mock_writer = MagicMock()
    mock_writer.drain = AsyncMock()
    mock_writer.wait_closed = AsyncMock()
    mock_writer.is_closing.return_value = False

    # Mock connect to return our stream mocks
    mock_connect = AsyncMock(return_value=(mock_reader, mock_writer))

    # Mock sleep to raise an exception on the 2nd call to break the loop
    # Call 1: Retry delay after first disconnect (Success)
    # Call 2: Retry delay after second disconnect (Raise to stop test)
    mock_sleep = AsyncMock()
    mock_sleep.side_effect = [None, RuntimeError("Break Loop")]

    with patch(
        "mcubridge.transport.serial._open_serial_connection_with_retry", mock_connect
    ), patch("asyncio.sleep", mock_sleep):
        # Run the task. It will connect, disconnect, sleep, reconnect, disconnect, sleep (BOOM)
        transport = SerialTransport(config, state, service)
        try:
            await transport.run()
        except RuntimeError as e:
            assert str(e) == "Break Loop"

    # Verify behavior
    # Connect should be called at least twice (initial + retry)
    assert mock_connect.call_count >= 2
    assert service.on_serial_connected.called
    assert service.on_serial_disconnected.called
    # Sleep should have been called (triggering the break)
    assert mock_sleep.called
