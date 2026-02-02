"""Tests to reach 100% code coverage."""

import asyncio
import struct
import logging
import pytest
from unittest import mock

from mcubridge.services import serial_flow
from mcubridge.state.context import RuntimeState
from mcubridge.rpc import protocol
from mcubridge.rpc.protocol import Command, Status
from mcubridge.config.settings import RuntimeConfig
from mcubridge.services.components.process import ProcessComponent
from mcubridge.transport.serial_fast import BridgeSerialProtocol


@pytest.mark.asyncio
async def test_serial_flow_rle_compression_coverage():
    """Cover RLE compression paths in SerialFlowController."""
    mock_sender = mock.AsyncMock(return_value=True)
    pipeline = serial_flow.SerialFlowController(
        ack_timeout=0.1,
        response_timeout=0.1,
        max_attempts=1,
        logger=logging.getLogger("test")
    )
    pipeline.set_sender(mock_sender)

    large_payload = b'A' * 20
    await pipeline.send(Command.CMD_CONSOLE_WRITE.value, large_payload)

    sent_cmd = mock_sender.call_args[0][0]
    assert sent_cmd & protocol.CMD_FLAG_COMPRESSED


@pytest.mark.asyncio
async def test_serial_flow_ack_mismatch_and_status_coverage():
    """Cover ACK and status handling edge cases."""
    pipeline = serial_flow.SerialFlowController(
        ack_timeout=0.1,
        response_timeout=0.1,
        max_attempts=1,
        logger=logging.getLogger("test")
    )
    mock_sender = mock.AsyncMock(return_value=True)
    pipeline.set_sender(mock_sender)

    task = asyncio.create_task(pipeline.send(Command.CMD_SET_PIN_MODE.value, b'\x01\x01'))
    await asyncio.sleep(0.1)

    pending = pipeline._current
    assert pending is not None

    mismatched_ack_payload = struct.pack(">H", 0x1234)
    pipeline.on_frame_received(Status.ACK.value, mismatched_ack_payload)
    assert not pending.ack_received

    # Payload that looks like a log message (printable chars) to trigger ignore branch
    pipeline.on_frame_received(Status.MALFORMED.value, b"Some log")
    assert pending.failure_status is None

    task.cancel()


@pytest.mark.asyncio
async def test_runtime_state_spool_failure_coverage(tmp_path):
    """Cover spool failure branches in context.py."""
    state = RuntimeState()
    state.mqtt_spool_dir = str(tmp_path)

    # Since it has slots, we can't just replace mqtt_spool with a Mock if it's already there
    # But we can mock the class or its methods if we are careful.
    with mock.patch("mcubridge.state.context.MQTTPublishSpool") as mock_spool_class:
        mock_spool = mock_spool_class.return_value
        mock_spool.append.side_effect = OSError("Disk full")
        state.mqtt_spool = mock_spool

        from mcubridge.mqtt.messages import QueuedPublish
        msg = QueuedPublish("topic", b"payload")
        res = await state.stash_mqtt_message(msg)
        assert res is False
        assert state.mqtt_spool_degraded is True


@pytest.mark.asyncio
async def test_process_component_error_coverage():
    """Cover error branches in process.py."""
    config = RuntimeConfig(serial_shared_secret=b"test_secret_123")
    config.process_max_concurrent = 4

    mock_state = mock.Mock()
    mock_state.process_timeout = 30
    mock_state.process_max_concurrent = 4
    mock_ctx = mock.AsyncMock()
    comp = ProcessComponent(config=config, state=mock_state, ctx=mock_ctx)

    with mock.patch("asyncio.create_subprocess_shell", side_effect=OSError("Boom")):
        await comp.handle_run(b"ls")
        mock_ctx.send_frame.assert_called()
        args = mock_ctx.send_frame.call_args[0]
        assert args[0] == Status.ERROR.value


@pytest.mark.asyncio
async def test_process_wait_timeout_coverage():
    """Cover TimeoutError in _wait_for_sync_completion."""
    config = RuntimeConfig(serial_shared_secret=b"test_secret_123")
    config.process_max_concurrent = 4

    mock_state = mock.Mock()
    mock_state.process_timeout = 30
    mock_ctx = mock.AsyncMock()
    comp = ProcessComponent(config=config, state=mock_state, ctx=mock_ctx)
    mock_proc = mock.AsyncMock()

    with mock.patch("asyncio.timeout", side_effect=asyncio.TimeoutError):
        res = await comp._wait_for_sync_completion(mock_proc, 123)
        assert res is True


@pytest.mark.asyncio
async def test_serial_fast_protocol_error_coverage():
    """Cover error branches in BridgeSerialProtocol."""
    mock_service = mock.AsyncMock()
    state = RuntimeState()
    loop = asyncio.get_running_loop()
    proto = BridgeSerialProtocol(mock_service, state, loop)

    # 1. ValueError containing "crc mismatch"
    # We must patch Frame.from_bytes where it is CALLED, which is in mcubridge.transport.serial_fast
    with mock.patch("mcubridge.transport.serial_fast.Frame.from_bytes", side_effect=ValueError("crc mismatch")):
        # We need to provide something that COBS can decode, or mock cobs.decode
        with mock.patch("mcubridge.transport.serial_fast.cobs.decode", return_value=b"some bytes"):
            await proto._async_process_packet(b"something")
            assert state.serial_crc_errors == 1

    # 2. Generic Decode Error
    with mock.patch("mcubridge.transport.serial_fast.Frame.from_bytes", side_effect=ValueError("other")):
        with mock.patch("mcubridge.transport.serial_fast.cobs.decode", return_value=b"some bytes"):
            await proto._async_process_packet(b"something")
            assert state.serial_decode_errors == 2
