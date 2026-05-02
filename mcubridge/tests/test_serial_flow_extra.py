"""Extra coverage for mcubridge.services.serial_flow."""

from __future__ import annotations

import msgspec
import logging
from unittest.mock import AsyncMock, patch
import pytest
from mcubridge.protocol.protocol import Command, Status
from mcubridge.protocol.structures import AckPacket, PendingCommand
from mcubridge.services.serial_flow import SerialFlowController


@pytest.mark.asyncio
async def test_serial_flow_compression_failure() -> None:
    flow = SerialFlowController(
        ack_timeout=0.1,
        response_timeout=0.2,
        max_attempts=1,
        logger=logging.getLogger("test"),
    )
    # Mock rle.should_compress to True, but rle.encode to raise
    with (
        patch("mcubridge.protocol.rle.should_compress", return_value=True),
        patch(
            "mcubridge.protocol.rle.RLE_TRANSFORM.build", side_effect=ValueError("fail")
        ),
    ):
        flow.set_sender(AsyncMock(return_value=True))
        # Not tracking this command to reach compression block easily
        await flow.send(0xFF, b"payload")
        # Should log warning and send uncompressed


@pytest.mark.asyncio
async def test_serial_flow_on_frame_ack_mismatch() -> None:
    flow = SerialFlowController(
        ack_timeout=0.1,
        response_timeout=0.2,
        max_attempts=1,
        logger=logging.getLogger("test"),
    )
    pending = PendingCommand(command_id=Command.CMD_GET_VERSION.value)
    flow._current = pending  # type: ignore[reportPrivateUsage]

    # ACK for different command
    payload = msgspec.msgpack.encode(
        AckPacket(command_id=Command.CMD_GET_FREE_MEMORY.value)
    )
    flow.on_frame_received(Status.ACK.value, 0, payload)
    assert not pending.ack_received


@pytest.mark.asyncio
async def test_serial_flow_on_frame_failure_human_readable() -> None:
    flow = SerialFlowController(
        ack_timeout=0.1,
        response_timeout=0.2,
        max_attempts=1,
        logger=logging.getLogger("test"),
    )
    pending = PendingCommand(command_id=Command.CMD_GET_VERSION.value)
    flow._current = pending  # type: ignore[reportPrivateUsage]

    # Human readable error (all printable ASCII) should be ignored
    flow.on_frame_received(Status.ERROR.value, 0, b"some error")
    assert pending.success is None


@pytest.mark.asyncio
async def test_serial_flow_send_and_wait_write_fail() -> None:
    flow = SerialFlowController(
        ack_timeout=0.1,
        response_timeout=0.2,
        max_attempts=1,
        logger=logging.getLogger("test"),
    )
    pending = PendingCommand(command_id=Command.CMD_GET_VERSION.value)
    sender = AsyncMock(return_value=False)

    with pytest.raises(SerialFlowController._FatalSerialError):  # type: ignore[reportPrivateUsage]
        await flow._send_and_wait(  # type: ignore[reportPrivateUsage]
            pending, b"p", sender, Command.CMD_GET_VERSION.value
        )


@pytest.mark.asyncio
async def test_serial_flow_send_and_wait_completion_set_during_timeout() -> None:
    flow = SerialFlowController(
        ack_timeout=0.1,
        response_timeout=0.2,
        max_attempts=1,
        logger=logging.getLogger("test"),
    )
    pending = PendingCommand(command_id=Command.CMD_GET_VERSION.value)
    sender = AsyncMock(return_value=True)

    # Set completion and success manually to simulate race/early success
    pending.completion.set()
    pending.success = True
    await flow._send_and_wait(pending, b"p", sender, Command.CMD_GET_VERSION.value)  # type: ignore[reportPrivateUsage]
    # Should return without raising TimeoutError
