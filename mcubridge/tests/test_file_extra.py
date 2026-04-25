"""Extra unit tests for FileComponent (SIL-2)."""

from __future__ import annotations

import os
import time
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from mcubridge.config.settings import RuntimeConfig
from mcubridge.services.runtime import BridgeService
from mcubridge.state.context import create_runtime_state
from mcubridge.services.file import FileComponent
from mcubridge.mqtt.spool_manager import MqttSpoolManager


@pytest.mark.asyncio
async def test_file_refresh_storage_usage_handles_oserror() -> None:
    config = RuntimeConfig(
        allow_non_tmp_paths=True,
        serial_shared_secret=b"secret_1234",
        file_system_root=f".tmp_tests/mcubridge-test-{os.getpid()}-{time.time_ns()}",
    )
    state = create_runtime_state(config)
    try:
        from mcubridge.services.serial_flow import SerialFlowController

        serial_flow = MagicMock(spec=SerialFlowController)

        service = BridgeService(config, state, MagicMock(spec=MqttSpoolManager))
        service.publish = AsyncMock()  # Mock direct publish

        comp = FileComponent(config, state, serial_flow, service)

        # Simulate OS error on statvfs
        with patch("os.statvfs", side_effect=OSError("stat-fail")):
            await comp._refresh_storage_usage()  # type: ignore

        # Should not have published anything if stat failed
        assert not service.publish.called
    finally:
        state.cleanup()
