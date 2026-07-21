"""Tests for serial transport resiliency."""

from mcubridge.services.runtime import BridgeService
from mcubridge.state.context import RuntimeState

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

    connect_count = 0

    async def mock_connect():
        nonlocal connect_count
        connect_count += 1
        if connect_count >= 2:
            raise asyncio.CancelledError()
        raise ConnectionError("Serial connection lost")

    with (
        patch.object(SerialTransport, "_connect_and_run", side_effect=mock_connect),
        patch("asyncio.sleep", AsyncMock()),
    ):
        transport = SerialTransport(config, state, service)
        await transport.run()

    assert connect_count >= 2
