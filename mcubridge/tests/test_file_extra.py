"""Extra tests for FileComponent coverage."""

from __future__ import annotations
from mcubridge.services.serial_flow import SerialFlowController

from pathlib import Path
from typing import Any, cast
from unittest.mock import AsyncMock, patch, MagicMock

import pytest
from mcubridge.config.settings import RuntimeConfig
from mcubridge.state.context import RuntimeState
from mcubridge.services.file import FileComponent
from mcubridge.protocol.structures import (
    FileWritePacket,
    FileReadPacket,
    FileRemovePacket,
)
from mcubridge.protocol.protocol import FileAction
import msgspec
import asyncio


@pytest.fixture
def file_comp(
    runtime_config: RuntimeConfig, runtime_state: RuntimeState
) -> FileComponent:
    serial_flow = AsyncMock(spec=SerialFlowController)
    mqtt_flow = AsyncMock()
    return FileComponent(runtime_config, runtime_state, serial_flow, mqtt_flow)


@pytest.mark.asyncio
async def test_file_write_with_quota_large_warning(file_comp: FileComponent) -> None:
    # Set quota very small to trigger warning
    file_comp.config.file_write_max_bytes = 10
    with (
        patch("mcubridge.services.file.logger") as mock_logger,
        patch("os.replace"),
    ):
        # Pass a Path object as expected by the type hint
        await getattr(file_comp, "_write_with_quota")(Path("test.txt"), b"A" * 1000)
        assert mock_logger.warning.called


@pytest.mark.asyncio
async def test_handle_write_invalid_path(file_comp: FileComponent) -> None:
    payload = msgspec.msgpack.encode(FileWritePacket(path="../invalid", data=b"data"))
    ok = await file_comp.handle_write(0, payload)
    assert not ok


@pytest.mark.asyncio
async def test_handle_write_quota_exceeded(file_comp: FileComponent) -> None:
    payload = msgspec.msgpack.encode(
        FileWritePacket(path="test.txt", data=b"A" * 10000000)
    )
    # mock config max write bytes
    file_comp.config.file_write_max_bytes = 10
    ok = await file_comp.handle_write(0, payload)
    assert not ok


@pytest.mark.asyncio
async def test_handle_write_decode_error(file_comp: FileComponent) -> None:
    ok = await file_comp.handle_write(0, b"bad data")
    assert not ok


@pytest.mark.asyncio
async def test_handle_read_invalid_path(file_comp: FileComponent) -> None:
    payload = msgspec.msgpack.encode(FileReadPacket(path="../invalid"))
    await file_comp.handle_read(0, payload)
    cast(AsyncMock, file_comp.serial_flow.send).assert_called()


@pytest.mark.asyncio
async def test_handle_read_decode_error(file_comp: FileComponent) -> None:
    await file_comp.handle_read(0, b"bad data")
    cast(AsyncMock, file_comp.serial_flow.send).assert_called()


@pytest.mark.asyncio
async def test_handle_remove_invalid_path(file_comp: FileComponent) -> None:
    payload = msgspec.msgpack.encode(FileRemovePacket(path="../invalid"))
    ok = await file_comp.handle_remove(0, payload)
    assert not ok


@pytest.mark.asyncio
async def test_handle_remove_decode_error(file_comp: FileComponent) -> None:
    ok = await file_comp.handle_remove(0, b"bad data")
    assert not ok


@pytest.mark.asyncio
async def test_handle_read_response_no_pending(file_comp: FileComponent) -> None:
    ok = await file_comp.handle_read_response(0, b"")
    assert not ok


@pytest.mark.asyncio
async def test_handle_read_response_decode_error(file_comp: FileComponent) -> None:
    fut = asyncio.get_running_loop().create_future()
    setattr(file_comp, "_pending_mcu_read", MagicMock(future=fut))
    ok = await file_comp.handle_read_response(0, b"bad data")
    assert not ok
    assert fut.done() and isinstance(fut.exception(), ValueError)


@pytest.mark.asyncio
async def test_handle_mqtt_write_invalid_path(file_comp: FileComponent) -> None:
    inbound = MagicMock()
    ok = await getattr(file_comp, "_handle_mqtt_write")(inbound, "../invalid", b"")
    assert not ok


@pytest.mark.asyncio
async def test_handle_mqtt_read_invalid_path(file_comp: FileComponent) -> None:
    inbound = MagicMock()
    ok = await getattr(file_comp, "_handle_mqtt_read")(inbound, "../invalid")
    assert ok  # Returns True but publishes error


@pytest.mark.asyncio
async def test_handle_mqtt_remove_invalid_path(file_comp: FileComponent) -> None:
    inbound = MagicMock()
    ok = await getattr(file_comp, "_handle_mqtt_remove")(inbound, "../invalid")
    assert not ok


@pytest.mark.asyncio
async def test_handle_mcu_write_disabled(file_comp: FileComponent) -> None:
    setattr(file_comp, "_mcu_backend_enabled", False)
    inbound = MagicMock()
    ok = await getattr(file_comp, "_handle_mcu_write")(inbound, "mcu/test.txt", b"")
    assert not ok


@pytest.mark.asyncio
async def test_handle_mcu_read_disabled(file_comp: FileComponent) -> None:
    setattr(file_comp, "_mcu_backend_enabled", False)
    inbound = MagicMock()
    ok = await getattr(file_comp, "_handle_mcu_read")(inbound, "mcu/test.txt")
    assert not ok


@pytest.mark.asyncio
async def test_handle_mcu_remove_disabled(file_comp: FileComponent) -> None:
    setattr(file_comp, "_mcu_backend_enabled", False)
    inbound = MagicMock()
    ok = await getattr(file_comp, "_handle_mcu_remove")(inbound, "mcu/test.txt")
    assert not ok


@pytest.mark.asyncio
async def test_handle_mcu_read_timeout(file_comp: FileComponent) -> None:
    file_comp.state.serial_response_timeout_ms = 1
    cast(AsyncMock, file_comp.serial_flow.send).return_value = True
    inbound = MagicMock()
    ok = await getattr(file_comp, "_handle_mcu_read")(inbound, "mcu/test.txt")
    assert not ok


@pytest.mark.asyncio
async def test_handle_mcu_write_invalid_identifier(file_comp: FileComponent) -> None:
    inbound = MagicMock()
    ok = await getattr(file_comp, "_handle_mcu_write")(inbound, "mcu/../invalid", b"")
    assert not ok


@pytest.mark.asyncio
async def test_handle_mqtt_invalid_action(file_comp: FileComponent) -> None:
    route = MagicMock(action=None, remainder=["test"])
    inbound = MagicMock()
    ok = await file_comp.handle_mqtt(route, inbound)
    assert not ok


@pytest.mark.asyncio
async def test_handle_mqtt_invalid_target(file_comp: FileComponent) -> None:
    route = MagicMock(action=FileAction.WRITE, remainder=[])
    inbound = MagicMock()
    ok = await file_comp.handle_mqtt(route, inbound)
    assert not ok
