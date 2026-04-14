"""Extra coverage for mcubridge.services.file."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import msgspec
import pytest
from mcubridge.config.settings import RuntimeConfig
from mcubridge.protocol import structures
from mcubridge.protocol.protocol import Status
from mcubridge.services.file import FileComponent  # type: ignore[reportPrivateUsage]
from mcubridge.state.context import create_runtime_state


@pytest.mark.asyncio
async def test_file_handle_write_traversal() -> None:
    config = RuntimeConfig(serial_shared_secret=b"1234", file_system_root="/tmp/mcubridge")
    state = create_runtime_state(config)
    ctx = MagicMock()
    ctx.send_frame = AsyncMock()

    component = FileComponent(config, state, ctx)

    # Path traversal attempt
    payload = msgspec.msgpack.encode(structures.FileWritePacket(path="../etc/passwd", data=b"data"))
    res = await component.handle_write(0, payload)

    assert res is False
    ctx.send_frame.assert_called_with(Status.ERROR.value, b"Invalid path")
