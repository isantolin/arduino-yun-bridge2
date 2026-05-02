"""Extra coverage for FileComponent (SIL-2)."""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import msgspec
import pytest

from mcubridge.config.settings import RuntimeConfig
from mcubridge.protocol import structures
from mcubridge.protocol.protocol import Command, Status
from mcubridge.services.file import FileComponent
from mcubridge.services.serial_flow import SerialFlowController
from mcubridge.state.context import create_runtime_state


@pytest.fixture
def file_comp(runtime_config: RuntimeConfig) -> FileComponent:
    state = create_runtime_state(runtime_config)
    serial_flow = AsyncMock(spec=SerialFlowController)
    serial_flow.send = AsyncMock(return_value=True)
    enqueue_mqtt = AsyncMock()
    comp = FileComponent(runtime_config, state, serial_flow, enqueue_mqtt)
    comp._get_storage_usage = MagicMock(return_value=0)  # type: ignore[reportPrivateUsage]
    return comp


@pytest.mark.asyncio
async def test_handle_mcu_read_timeout(file_comp: FileComponent) -> None:
    # MCU Backend must be enabled
    file_comp._mcu_backend_enabled = True  # type: ignore[reportPrivateUsage]
    
    # We don't trigger the response, so it should time out
    # [SIL-2] Using a very short timeout for testing
    with patch("asyncio.timeout", return_value=asyncio.timeout(0.1)):
        # We need to call _handle_mcu_read which is private but tested for coverage
        from aiomqtt.message import Message
        msg = MagicMock(spec=Message)
        msg.topic = "br/file/read/mcu/test.txt"
        msg.properties = None
        
        await file_comp._handle_mcu_read(msg, "mcu/test.txt")  # type: ignore[reportPrivateUsage]
        
    file_comp.enqueue_mqtt.assert_called()
    assert b"mcu_timeout" in file_comp.enqueue_mqtt.call_args.args[0].payload


@pytest.mark.asyncio
async def test_handle_write_quota_exceeded(file_comp: FileComponent) -> None:
    file_comp.config.file_storage_quota_bytes = 10
    
    # Mock storage usage to 100 (already exceeding 10)
    file_comp._get_storage_usage = MagicMock(return_value=100)  # type: ignore[reportPrivateUsage]
    
    pkt = structures.FileWritePacket(path="quota.txt", data=b"too much data")
    await file_comp.handle_write(0, msgspec.msgpack.encode(pkt))
        
    file_comp.serial_flow.send.assert_called_with(Status.ERROR.value, b"")
