"""Unit tests for ProcessComponent buffer management (SIL-2)."""

from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, MagicMock

import msgspec
import pytest

from mcubridge.config.settings import RuntimeConfig
from mcubridge.services.process import ProcessComponent
from mcubridge.services.serial_flow import SerialFlowController
from mcubridge.state.context import create_runtime_state


@pytest.fixture
def process_comp(runtime_config: RuntimeConfig) -> ProcessComponent:
    state = create_runtime_state(runtime_config)
    serial_flow = AsyncMock(spec=SerialFlowController)
    serial_flow.send = AsyncMock(return_value=True)
    enqueue_mqtt = AsyncMock()
    return ProcessComponent(runtime_config, state, serial_flow, enqueue_mqtt)


@pytest.mark.asyncio
async def test_run_async_supervises_process(process_comp: ProcessComponent) -> None:
    process_comp.state.allowed_policy = MagicMock()
    process_comp.state.allowed_policy.is_allowed.return_value = True

    # Run a real but short process
    pid = await process_comp.run_async("echo hello")
    assert pid > 0

    # Wait for it to finish
    for _ in range(10):
        if pid in process_comp.state.process_exit_codes:
            break
        await asyncio.sleep(0.1)

    assert pid in process_comp.state.process_exit_codes
    assert process_comp.state.process_exit_codes[pid] == 0

    # Check that MQTT was called with output
    process_comp.enqueue_mqtt.assert_called()
    # Find the output call
    found_output = False
    for call in process_comp.enqueue_mqtt.call_args_list:
        msg = call.args[0]
        if "stdout" in msg.topic_name:
            found_output = True
            payload = msgspec.msgpack.decode(msg.payload)
            assert payload["pid"] == pid
            assert b"hello" in payload["data"]

    assert found_output
