"""Extra coverage for mcubridge.services.serial_flow."""

from __future__ import annotations

from typing import Any, cast
from unittest.mock import AsyncMock, MagicMock

import msgspec
import pytest
from mcubridge.protocol import structures
from mcubridge.protocol.protocol import Command, Status
from mcubridge.services.serial_flow import SerialFlowController


@pytest.mark.asyncio
async def test_serial_flow_on_frame_ack_mismatch() -> None:
    sender = MagicMock()
    sender.send_frame = AsyncMock(return_value=True)

    flow = SerialFlowController(cast(Any, sender))  # type: ignore[reportCallIssue]

    # Use public API to trigger a pending command
    await flow.send_and_wait  # type: ignore[reportUnknownMemberType, reportAttributeAccessIssue](Command.CMD_GET_VERSION.value, b"")

    # Extract pending from internal state with type ignore for testing
    pending = flow._pending[0]  # type: ignore[reportPrivateUsage, reportUnknownMemberType]

    # ACK for different command
    payload = msgspec.msgpack.encode(structures.AckPacket(command_id=Command.CMD_GET_FREE_MEMORY.value))
    flow.on_frame_received(Status.ACK.value, 0, payload)
    assert not pending.ack_received  # type: ignore[reportUnknownMemberType]
