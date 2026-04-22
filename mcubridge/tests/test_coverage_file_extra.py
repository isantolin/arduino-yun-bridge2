from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any, cast
from unittest.mock import AsyncMock, patch

import pytest
import msgspec
from mcubridge.services.file import FileComponent
from mcubridge.state.context import create_runtime_state
from mcubridge.protocol.protocol import Status
from mcubridge.protocol.structures import (
    FileWritePacket, TopicRoute
)
from mcubridge.protocol.topics import Topic
from aiomqtt.message import Message

@pytest.fixture
def file_comp(runtime_config: Any, tmp_path: Path):
    runtime_config.file_system_root = str(tmp_path)
    state = create_runtime_state(runtime_config)
    comp = FileComponent(
        config=runtime_config,
        state=state,
        serial_flow=AsyncMock(),
        mqtt_flow=AsyncMock()
    )
    return comp

@pytest.mark.asyncio
async def test_handle_write_quota_exceeded(file_comp: FileComponent):
    file_comp.config.file_write_max_bytes = 5
    payload = msgspec.msgpack.encode(FileWritePacket(path="test.txt", data=b"too-long"))
    ok = await file_comp.handle_write(0, payload)
    assert ok is False
    cast(AsyncMock, file_comp.serial_flow.send).assert_called_with(Status.ERROR.value, b"Quota exceeded")

@pytest.mark.asyncio
async def test_handle_read_response_no_pending(file_comp: FileComponent):
    ok = await file_comp.handle_read_response(0, b"")
    assert ok is False

@pytest.mark.asyncio
async def test_handle_mqtt_unknown_action(file_comp: FileComponent):
    route = TopicRoute(raw="", prefix="br", topic=Topic.FILE, segments=("unknown", "file.txt"))
    msg = Message("br/file/unknown/file.txt", b"", 0, False, False, None)
    ok = await file_comp.handle_mqtt(route, msg)
    assert ok is False

@pytest.mark.asyncio
async def test_refresh_storage_usage_error(file_comp: FileComponent):
    file_comp.config.file_system_root = "/non/existent/path"
    await file_comp._refresh_storage_usage()
    assert file_comp.state.file_storage_bytes_used == 0

@pytest.mark.asyncio
async def test_handle_mcu_read_timeout(file_comp: FileComponent):
    # Trigger MQTT read from MCU
    file_comp._mcu_backend_enabled = True
    route = TopicRoute(raw="", prefix="br", topic=Topic.FILE, segments=("read", "mcu", "test.txt"))
    msg = Message("br/file/read/mcu/test.txt", b"", 0, False, False, None)

    # Force timeout in wait_for
    with patch("asyncio.wait_for", side_effect=asyncio.TimeoutError):
        ok = await file_comp.handle_mqtt(route, msg)
        assert ok is False
