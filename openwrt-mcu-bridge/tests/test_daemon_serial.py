"""Tests for serial transport resiliency."""

import asyncio
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from mcubridge.config.settings import RuntimeConfig
from mcubridge.protocol import protocol, structures
from mcubridge.transport import (
    SerialTransport,
)
from mcubridge.transport.serial import BridgeSerialProtocol


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

    # Mock Transport/Protocol
    mock_transport = MagicMock()
    mock_transport.close = MagicMock()
    # Simulate connection dropping: is_closing returns False once then True
    # Sequence: Loop Check (False), Loop Check (True), Finally Check (False), Loop Check (True), Finally Check (True)
    mock_transport.is_closing.side_effect = [False, True, False, True, True, True]

    mock_protocol = MagicMock(spec=BridgeSerialProtocol)
    mock_protocol.loop = MagicMock()
    mock_protocol.loop.create_future.return_value = asyncio.Future()
    mock_protocol.connected_future = asyncio.Future()
    mock_protocol.connected_future.set_result(None)

    # Mock create_serial_connection
    mock_create = AsyncMock(return_value=(mock_transport, mock_protocol))

    # Mock sleep to fast-forward loops and eventually break the run loop
    # 1. sleep(1) in _connect_and_run (connection 1 alive check)
    # 2. sleep(1) in run (reconnect delay)
    # 3. sleep(1) in _connect_and_run (connection 2 alive check)
    # 4. sleep(1) in run (reconnect delay) -> Raise Break Loop
    mock_sleep = AsyncMock()
    mock_sleep.side_effect = [None, None, None, RuntimeError("Break Loop")]

    with (
        patch("mcubridge.transport.serial.serial_asyncio_fast.create_serial_connection", mock_create),
        patch("asyncio.sleep", mock_sleep),
    ):
        transport = SerialTransport(config, state, service)
        try:
            await transport.run()
        except RuntimeError as e:
            assert str(e) == "Break Loop"

    # Verify behavior
    # Connect should be called at least twice (initial + retry)
    assert mock_create.call_count >= 2
    assert service.on_serial_connected.called
    assert service.on_serial_disconnected.called
