"""Tests for serial transport resiliency."""

from mcubridge.services.runtime import BridgeService
from mcubridge.state.context import RuntimeState
from typing import Any

import os
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
        cloud_host="localhost",
        cloud_port=1883,
        cloud_user=None,
        cloud_pass=None,
        cloud_tls=False,
        cloud_cafile=None,
        cloud_certfile=None,
        cloud_keyfile=None,
        topic_prefix="br",
        allowed_commands=(),
        file_system_root=os.path.abspath(".tmp_tests"),
        process_timeout=5,
        reconnect_delay=1,
        serial_shared_secret=b"s_e_c_r_e_t_mock",
        allow_non_tmp_paths=True,
    )
    state = AsyncMock(spec=RuntimeState)
    service = AsyncMock(spec=BridgeService)
    service.on_serial_connected = AsyncMock()
    service.on_serial_disconnected = AsyncMock()
    service.register_serial_sender = MagicMock()

    # Mock serialx.AsyncSerial
    mock_serial = AsyncMock()
    mock_serial.__aenter__.return_value = mock_serial
    mock_serial.__aexit__.return_value = None
    mock_serial.transport = AsyncMock()
    mock_serial.readuntil.side_effect = [
        asyncio.IncompleteReadError(b"", None),  # First connection lost
        asyncio.IncompleteReadError(b"", None),  # Second connection lost
        asyncio.IncompleteReadError(b"", None),  # Third connection lost
    ]

    mock_async_serial_cls = MagicMock(return_value=mock_serial)

    # Mock sleep to fast-forward loops and eventually break the run loop
    sleep_count = 0

    async def mock_sleep_fn(duration: Any):
        nonlocal sleep_count
        sleep_count += 1
        if sleep_count > 100:
            raise RuntimeError("Break Loop")
        return None

    mock_sleep = AsyncMock(side_effect=mock_sleep_fn)

    with (
        patch(
            "mcubridge.transport.serial.serialx.AsyncSerial",
            mock_async_serial_cls,
        ),
        patch("asyncio.sleep", mock_sleep),
        patch.object(SerialTransport, "_toggle_dtr", AsyncMock()),
    ):
        transport = SerialTransport(config, state, service)
        try:
            await transport.run()
        except RuntimeError as e:
            assert str(e) == "Break Loop"

    # Verify behavior
    # Connect should be called at least twice (initial + retry)
    assert mock_async_serial_cls.call_count >= 2
    assert service.on_serial_connected.called
    assert service.on_serial_disconnected.called
