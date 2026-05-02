"""Tests for the ProcessComponent."""

from __future__ import annotations

from unittest.mock import AsyncMock

import pytest
from mcubridge.config.settings import RuntimeConfig
from mcubridge.services.process import ProcessComponent
from mcubridge.services.serial_flow import SerialFlowController
from mcubridge.state.context import create_runtime_state


@pytest.mark.asyncio
async def test_process_run_async_limit_reached(
    runtime_config: RuntimeConfig,
) -> None:
    # Set limit to 0
    runtime_config.process_max_concurrent = 0
    state = create_runtime_state(runtime_config)
    try:
        serial_flow = AsyncMock(spec=SerialFlowController)
        enqueue_mqtt = AsyncMock()

        comp = ProcessComponent(runtime_config, state, serial_flow, enqueue_mqtt)

        # Attempt to run
        pid = await comp.run_async("ls")
        assert pid == 0
    finally:
        state.cleanup()
