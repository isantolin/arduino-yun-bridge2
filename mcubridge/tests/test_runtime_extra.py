"""Extra coverage for mcubridge.services.runtime."""

from __future__ import annotations

import os
import time
from unittest.mock import patch

import msgspec
import pytest
from mcubridge.config.settings import RuntimeConfig
from mcubridge.services.runtime import BridgeService
from mcubridge.state.context import create_runtime_state


@pytest.mark.asyncio
async def test_runtime_handle_ack_fallback() -> None:
    config = RuntimeConfig(
        serial_shared_secret=b"secret_1234",
        file_system_root=f"/tmp/mcubridge-test-{os.getpid()}-{time.time_ns()}",
    )
    state = create_runtime_state(config)
    try:
        service = BridgeService(config, state)

        # Payload valid length (2) but decode may fail for malformed data.
        # Let's try to trigger a failure in decoding by providing invalid data.
        with patch("msgspec.msgpack.decode", side_effect=msgspec.MsgspecError):
            await service._handle_ack(0, b"\xFF\xFF")  # type: ignore[reportPrivateUsage]

        # Handled gracefully, no exception escaped.
        assert True
    finally:
        state.cleanup()
