"""Unit tests for daemon serial connection management (SIL-2)."""

from __future__ import annotations

import asyncio
import contextlib
import time
from typing import Any, cast
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from mcubridge.config.settings import RuntimeConfig
from mcubridge.protocol import protocol
from mcubridge.transport.serial import SerialTransport
from mcubridge.services.runtime import BridgeService
from mcubridge.state.context import create_runtime_state


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
        reconnect_delay=0.01, # Fast retry
        serial_shared_secret=b"s_e_c_r_e_t_mock",
    )
    state = create_runtime_state(config)
    try:
        service = MagicMock(spec=BridgeService)
        service.on_serial_connected = AsyncMock()
        service.on_serial_disconnected = AsyncMock()
        service.register_serial_sender = MagicMock()
        service.serial_flow = MagicMock()
        service.serial_flow.negotiate_baudrate = AsyncMock(return_value=True)

        mock_reader = AsyncMock(spec=asyncio.StreamReader)
        # Endless hang without being a future that gets passed as 'packet'
        async def _hang(*args, **kwargs):
            while True:
                await asyncio.sleep(100)
        
        mock_reader.readuntil.side_effect = [
            asyncio.IncompleteReadError(b"", None),
            asyncio.IncompleteReadError(b"", None),
            asyncio.IncompleteReadError(b"", None),
            asyncio.IncompleteReadError(b"", None),
            asyncio.IncompleteReadError(b"", None),
            _hang,
        ]
    
        mock_writer = MagicMock(spec=asyncio.StreamWriter)
        mock_writer.is_closing.return_value = False
        mock_writer.wait_closed = AsyncMock()

        # Mock open_serial_connection
        mock_open = AsyncMock(return_value=(mock_reader, mock_writer))

        with (
            patch(
                "serial_asyncio_fast.open_serial_connection",
                mock_open,
            ),
            patch("mcubridge.transport.serial.serial.Serial", MagicMock()),
        ):
            transport = SerialTransport(config, state, cast(Any, service))
            
            # Start transport and wait for enough calls
            task = asyncio.create_task(transport.run())
            
            # Use a faster check and limited duration
            start_time = time.monotonic()
            while mock_open.call_count < 3 and (time.monotonic() - start_time) < 1.0:
                await asyncio.sleep(0.01)
            
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await task

        assert mock_open.call_count >= 3
    finally:
        state.cleanup()
