from __future__ import annotations
import msgspec
import logging
from unittest.mock import AsyncMock, patch
import pytest
from mcubridge.protocol.protocol import Command, Status
from mcubridge.protocol.structures import AckPacket, PendingCommand
from mcubridge.services.serial_flow import SerialFlowController

"""Extra coverage for mcubridge.services.serial_flow."""


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
async def test_serial_flow_send_write_fail() -> None:
    flow = SerialFlowController(
        ack_timeout=0.1,
        response_timeout=0.2,
        max_attempts=1,
        logger=logging.getLogger("test"),
    )
    flow.set_sender(AsyncMock(return_value=False))

    # Should return False on write failure
    ok = await flow.send(Command.CMD_GET_VERSION.value, b"p")
    assert ok is False


@pytest.mark.asyncio
async def test_serial_flow_send_success_after_early_completion() -> None:
    flow = SerialFlowController(
        ack_timeout=0.1,
        response_timeout=0.2,
        max_attempts=1,
        logger=logging.getLogger("test"),
    )

    command_id = Command.CMD_CONSOLE_WRITE.value

    async def mock_sender(cid: int, p: bytes) -> bool:
        # Simulate background response immediately
        flow.on_frame_received(Status.ACK.value, 0, b"")
        return True

    flow.set_sender(mock_sender)

    # Use a command that only requires ACK
    ok = await flow.send(command_id, b"p")
    assert ok is True
