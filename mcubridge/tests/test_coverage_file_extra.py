"""Extra coverage tests for mcubridge.services.file."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import msgspec
import pytest

from mcubridge.services.file import FileComponent
from mcubridge.protocol.protocol import Status, Command
from mcubridge.protocol.structures import (
    FileWritePacket,
    FileReadPacket,
    FileRemovePacket,
)


@pytest.mark.asyncio
async def test_file_write_failure(runtime_config):
    """Cover lines 83-85: Write failure."""
    state = MagicMock()
    # Ensure usage seeded
    state.file_storage_bytes_used = 0
    serial_flow = AsyncMock()
    mqtt_flow = MagicMock()
    file_comp = FileComponent(runtime_config, state, serial_flow, mqtt_flow)

    # Correct field name is 'data' not 'content'
    packet = FileWritePacket(path="test.txt", data=b"data")
    payload = msgspec.msgpack.encode(packet)

    # Patch pathlib.Path.write_bytes which is used in _write_with_quota
    with patch("pathlib.Path.write_bytes", side_effect=OSError("Disk full")):
        await file_comp.handle_write(1, payload)

    serial_flow.send.assert_called()
    # Check if Status.ERROR (49) was sent
    assert serial_flow.send.call_args[0][0] == Status.ERROR.value


@pytest.mark.asyncio
async def test_file_read_empty(runtime_config, tmp_path):
    """Cover lines 111-114: Read empty file."""
    # Setup safe path
    runtime_config = msgspec.structs.replace(
        runtime_config, file_system_root=str(tmp_path), allow_non_tmp_paths=True
    )
    state = MagicMock()
    state.file_storage_bytes_used = 0
    serial_flow = AsyncMock()
    mqtt_flow = MagicMock()
    file_comp = FileComponent(runtime_config, state, serial_flow, mqtt_flow)

    test_file = tmp_path / "empty.txt"
    test_file.touch()

    packet = FileReadPacket(path="empty.txt")
    payload = msgspec.msgpack.encode(packet)
    await file_comp.handle_read(1, payload)

    # Check if CMD_FILE_READ_RESP was sent with empty content
    serial_flow.send.assert_called()
    call_args = serial_flow.send.call_args
    assert call_args[0][0] == Command.CMD_FILE_READ_RESP.value


@pytest.mark.asyncio
async def test_file_remove_failure(runtime_config):
    """Cover lines 147-151: Remove failure."""
    state = MagicMock()
    state.file_storage_bytes_used = 0
    serial_flow = AsyncMock()
    mqtt_flow = MagicMock()
    file_comp = FileComponent(runtime_config, state, serial_flow, mqtt_flow)

    with patch.object(
        file_comp, "_get_safe_path", side_effect=OSError("Permission denied")
    ):
        packet = FileRemovePacket(path="test.txt")
        payload = msgspec.msgpack.encode(packet)
        await file_comp.handle_remove(1, payload)

    serial_flow.send.assert_called()
    assert serial_flow.send.call_args[0][0] == Status.ERROR.value
