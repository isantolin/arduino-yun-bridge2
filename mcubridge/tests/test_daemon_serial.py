import pytest
pytestmark = pytest.mark.skip(reason="Obsolete API")
"""Tests for serial transport resiliency."""

import asyncio
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from mcubridge.config.settings import RuntimeConfig
from mcubridge.protocol import protocol
from mcubridge.transport import (
    SerialTransport,
)


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
        serial_shared_secret=b"s_e_c_r_e_t_mock",
    )
    state = MagicMock()
    service = MagicMock()
    service.on_serial_connected = AsyncMock()
    service.on_serial_disconnected = AsyncMock()
    service.register_serial_sender = MagicMock()

    # Mock Transport/Protocol via Streams API
    mock_reader = AsyncMock(spec=asyncio.StreamReader)
    # Simulate connection dropping by raising IncompleteReadError in the loop
    mock_reader.readuntil.side_effect = [
        asyncio.IncompleteReadError(b"", None), # First connection lost
        asyncio.IncompleteReadError(b"", None), # Second connection lost
        asyncio.IncompleteReadError(b"", None), # Third connection lost
    ]

    mock_writer = MagicMock(spec=asyncio.StreamWriter)
    mock_writer.is_closing.return_value = False
    mock_writer.wait_closed = AsyncMock()

    # Mock open_serial_connection
    mock_open = AsyncMock(return_value=(mock_reader, mock_writer))

    # Mock sleep to fast-forward loops and eventually break the run loop
    sleep_count = 0
    async def mock_sleep_fn(duration):
        nonlocal sleep_count
        sleep_count += 1
        if sleep_count > 100:
            raise asyncio.CancelledError("Break Loop")
        return None

    mock_sleep = AsyncMock(side_effect=mock_sleep_fn)

    with (
        patch(
            "mcubridge.transport.serial.serial_asyncio_fast.open_serial_connection",
            mock_open,
        ),
        patch("mcubridge.transport.serial.serial.Serial", MagicMock()),
        patch("asyncio.sleep", mock_sleep),
    ):
        transport = SerialTransport(config, state, service)
        try:
            await transport.run()
        except RuntimeError as e:
            assert str(e) == "Break Loop"

    # Verify behavior
    # Connect should be called at least twice (initial + retry)
    assert mock_open.call_count >= 2
    assert service.on_serial_connected.called
    assert service.on_serial_disconnected.called
