# pyright: reportPrivateUsage=false
"""Surgical coverage tests for BridgeService MCU handlers. [SIL-2]"""

from __future__ import annotations

import asyncio
import os
import time
from unittest.mock import AsyncMock, patch

import pytest

from collections.abc import Iterator
from mcubridge.config.settings import RuntimeConfig
from mcubridge.protocol import mcubridge_pb2 as pb
from mcubridge.protocol.protocol import Command, Status
from mcubridge.protocol.structures import PendingPinRequest
from mcubridge.services.runtime import BridgeService
from mcubridge.state.context import RuntimeState, create_runtime_state
from mcubridge.transport.serial import SerialTransport


def _make_config() -> RuntimeConfig:
    fs_root = f".tmp_tests/rt-mcu-fs-{os.getpid()}-{time.time_ns()}"
    spool = f".tmp_tests/rt-mcu-spool-{os.getpid()}-{time.time_ns()}"
    os.makedirs(fs_root, exist_ok=True)
    os.makedirs(spool, exist_ok=True)
    return RuntimeConfig(
        allowed_commands=("echo", "ls"),
        serial_shared_secret=b"testshared",
        file_system_root=fs_root,
        cloud_spool_dir=spool,
        allow_non_tmp_paths=True,
    )


@pytest.fixture
def svc() -> Iterator[tuple[BridgeService, RuntimeState, AsyncMock]]:
    config = _make_config()
    state = create_runtime_state(config)
    state.state = "synchronized"
    state.link_sync_event.set()
    state.serial_tx_allowed.set()
    serial = AsyncMock(spec=SerialTransport)
    serial.send.return_value = True
    serial.send_raw.return_value = True
    service = BridgeService(config, state, serial)
    try:
        yield service, state, serial
    finally:
        service.cleanup()
        state.cleanup()


# ---------------------------------------------------------------------------
# _unsupported_mcu_request
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_unsupported_mcu_request_no_serial(
    svc: tuple[BridgeService, RuntimeState, AsyncMock],
) -> None:
    service, _state, _serial = svc
    service.serial = None  # type: ignore[assignment]
    result = await service._unsupported_mcu_request(1, None)  # type: ignore[arg-type]
    assert result is False


@pytest.mark.asyncio
async def test_unsupported_mcu_request_sends(
    svc: tuple[BridgeService, RuntimeState, AsyncMock],
) -> None:
    service, _state, serial = svc
    serial.send.return_value = True
    result = await service._unsupported_mcu_request(1, None, msg="test_msg")
    assert result is True


# ---------------------------------------------------------------------------
# _on_mcu_mailbox_available
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_on_mcu_mailbox_available_no_serial(
    svc: tuple[BridgeService, RuntimeState, AsyncMock],
) -> None:
    service, _state, _serial = svc
    service.serial = None  # type: ignore[assignment]
    result = await service._on_mcu_mailbox_available(1, None)
    assert result is False


@pytest.mark.asyncio
async def test_on_mcu_mailbox_available_with_items(
    svc: tuple[BridgeService, RuntimeState, AsyncMock],
) -> None:
    service, state, serial = svc
    await state.mailbox_queue.append(b"hello")
    serial.send.return_value = True
    result = await service._on_mcu_mailbox_available(1, None)
    assert result is True
    serial.send.assert_called_once()


# ---------------------------------------------------------------------------
# _on_mcu_mailbox_read
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_on_mcu_mailbox_read_no_serial(
    svc: tuple[BridgeService, RuntimeState, AsyncMock],
) -> None:
    service, _state, _serial = svc
    service.serial = None  # type: ignore[assignment]
    result = await service._on_mcu_mailbox_read(1, None)
    assert result is False


@pytest.mark.asyncio
async def test_on_mcu_mailbox_read_empty_queue(
    svc: tuple[BridgeService, RuntimeState, AsyncMock],
) -> None:
    service, _state, serial = svc
    serial.send.return_value = True
    # Queue is empty — IndexError path sets content = b""
    result = await service._on_mcu_mailbox_read(1, None)
    assert result is True


@pytest.mark.asyncio
async def test_on_mcu_mailbox_read_with_content(
    svc: tuple[BridgeService, RuntimeState, AsyncMock],
) -> None:
    service, state, serial = svc
    await state.mailbox_queue.append(b"data_payload")
    serial.send.return_value = True
    result = await service._on_mcu_mailbox_read(1, None)
    assert result is True
    args = serial.send.call_args[0]
    assert args[0] == Command.CMD_MAILBOX_READ_RESP.value


# ---------------------------------------------------------------------------
# _on_mcu_mailbox_processed
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_on_mcu_mailbox_processed(
    svc: tuple[BridgeService, RuntimeState, AsyncMock],
) -> None:
    service, _state, _serial = svc
    p = pb.MailboxProcessed(message_id=1)
    captured: list[pb.CloudQueuedPublish] = []

    async def _cap(msg: pb.CloudQueuedPublish, *, reply_context: object = None) -> None:
        captured.append(msg)

    with patch.object(service, "enqueue_cloud", side_effect=_cap):
        await service._on_mcu_mailbox_processed(1, p)
    assert captured


# ---------------------------------------------------------------------------
# _on_mcu_file_write
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_on_mcu_file_write_no_serial(
    svc: tuple[BridgeService, RuntimeState, AsyncMock],
) -> None:
    service, _state, _serial = svc
    service.serial = None  # type: ignore[assignment]
    p = pb.FileWrite(path="test.txt", data=b"x")
    result = await service._on_mcu_file_write(1, p)
    assert result is False


@pytest.mark.asyncio
async def test_on_mcu_file_write_unsafe_path(
    svc: tuple[BridgeService, RuntimeState, AsyncMock],
) -> None:
    service, _state, serial = svc
    serial.send.return_value = True
    p = pb.FileWrite(path="/etc/passwd", data=b"x")
    result = await service._on_mcu_file_write(1, p)
    # unsafe path → _get_safe_path returns None → ERROR branch
    assert result is True
    assert serial.send.called


@pytest.mark.asyncio
async def test_on_mcu_file_write_success(
    svc: tuple[BridgeService, RuntimeState, AsyncMock],
) -> None:
    service, _state, serial = svc
    serial.send.return_value = True
    p = pb.FileWrite(path="safe.txt", data=b"content")
    result = await service._on_mcu_file_write(1, p)
    assert result is True


# ---------------------------------------------------------------------------
# _on_mcu_file_read
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_on_mcu_file_read_no_serial(
    svc: tuple[BridgeService, RuntimeState, AsyncMock],
) -> None:
    service, _state, _serial = svc
    service.serial = None  # type: ignore[assignment]
    p = pb.FileRead(path="test.txt")
    result = await service._on_mcu_file_read(1, p)
    assert result is None


@pytest.mark.asyncio
async def test_on_mcu_file_read_missing_file(
    svc: tuple[BridgeService, RuntimeState, AsyncMock],
) -> None:
    service, _state, serial = svc
    serial.send.return_value = True
    p = pb.FileRead(path="nonexistent.txt")
    await service._on_mcu_file_read(1, p)
    args = serial.send.call_args[0]
    assert args[0] == Status.ERROR.value


@pytest.mark.asyncio
async def test_on_mcu_file_read_existing_nonempty(
    svc: tuple[BridgeService, RuntimeState, AsyncMock],
) -> None:
    service, _state, serial = svc
    serial.send.return_value = True
    # Write a real file into the sandbox
    fs_root = service.config.file_system_root
    (__import__("pathlib").Path(fs_root) / "read_test.txt").write_bytes(b"hello world")
    p = pb.FileRead(path="read_test.txt")
    await service._on_mcu_file_read(1, p)
    assert serial.send.called


@pytest.mark.asyncio
async def test_on_mcu_file_read_existing_empty(
    svc: tuple[BridgeService, RuntimeState, AsyncMock],
) -> None:
    service, _state, serial = svc
    serial.send.return_value = True
    fs_root = service.config.file_system_root
    (__import__("pathlib").Path(fs_root) / "empty.txt").write_bytes(b"")
    p = pb.FileRead(path="empty.txt")
    await service._on_mcu_file_read(1, p)
    assert serial.send.called
    args = serial.send.call_args[0]
    assert args[0] == Command.CMD_FILE_READ_RESP.value


# ---------------------------------------------------------------------------
# _on_mcu_file_remove
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_on_mcu_file_remove_no_serial(
    svc: tuple[BridgeService, RuntimeState, AsyncMock],
) -> None:
    service, _state, _serial = svc
    service.serial = None  # type: ignore[assignment]
    p = pb.FileRemove(path="x.txt")
    result = await service._on_mcu_file_remove(1, p)
    assert result is False


@pytest.mark.asyncio
async def test_on_mcu_file_remove_missing(
    svc: tuple[BridgeService, RuntimeState, AsyncMock],
) -> None:
    service, _state, serial = svc
    serial.send.return_value = True
    p = pb.FileRemove(path="missing.txt")
    result = await service._on_mcu_file_remove(1, p)
    assert result is True
    args = serial.send.call_args[0]
    assert args[0] == Status.ERROR.value


@pytest.mark.asyncio
async def test_on_mcu_file_remove_existing(
    svc: tuple[BridgeService, RuntimeState, AsyncMock],
) -> None:
    service, _state, serial = svc
    serial.send.return_value = True
    fs_root = service.config.file_system_root
    (__import__("pathlib").Path(fs_root) / "del.txt").write_bytes(b"bye")
    p = pb.FileRemove(path="del.txt")
    result = await service._on_mcu_file_remove(1, p)
    assert result is True
    args = serial.send.call_args[0]
    assert args[0] == Status.OK.value


# ---------------------------------------------------------------------------
# _on_mcu_file_read_resp
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_on_mcu_file_read_resp_no_pending(
    svc: tuple[BridgeService, RuntimeState, AsyncMock],
) -> None:
    service, _state, _serial = svc
    service._pending_mcu_read = None
    p = pb.FileReadResponse(content=b"chunk")
    result = await service._on_mcu_file_read_resp(1, p)
    assert result is False


@pytest.mark.asyncio
async def test_on_mcu_file_read_resp_accumulates_chunks(
    svc: tuple[BridgeService, RuntimeState, AsyncMock],
) -> None:
    service, _state, _serial = svc
    loop = asyncio.get_event_loop()
    from mcubridge.services.runtime import _PendingMcuRead

    pending = _PendingMcuRead(loop.create_future())
    service._pending_mcu_read = pending
    p = pb.FileReadResponse(content=b"chunk1")
    result = await service._on_mcu_file_read_resp(1, p)
    assert result is True
    assert pending.chunks == [b"chunk1"]


@pytest.mark.asyncio
async def test_on_mcu_file_read_resp_completes_future(
    svc: tuple[BridgeService, RuntimeState, AsyncMock],
) -> None:
    service, _state, _serial = svc
    loop = asyncio.get_event_loop()
    from mcubridge.services.runtime import _PendingMcuRead

    pending = _PendingMcuRead(loop.create_future())
    pending.chunks = [b"a", b"b"]
    service._pending_mcu_read = pending
    p = pb.FileReadResponse(content=b"")  # empty → signal EOF
    result = await service._on_mcu_file_read_resp(1, p)
    assert result is True
    assert pending.future.result() == b"ab"


# ---------------------------------------------------------------------------
# _on_mcu_ack
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_on_mcu_ack_valid(
    svc: tuple[BridgeService, RuntimeState, AsyncMock],
) -> None:
    service, _state, _serial = svc
    p = pb.AckPacket(command_id=0x01)
    with patch("mcubridge.services.runtime.logger.debug") as mock_debug:
        await service._on_mcu_ack(1, p)
        mock_debug.assert_called_once_with("MCU ACK received", command_id="0x01")


@pytest.mark.asyncio
async def test_on_mcu_ack_raw_bytes(
    svc: tuple[BridgeService, RuntimeState, AsyncMock],
) -> None:
    service, _state, _serial = svc
    raw = pb.AckPacket(command_id=0x02).SerializeToString()
    with patch("mcubridge.services.runtime.logger.debug") as mock_debug:
        await service._on_mcu_ack(1, raw)
        mock_debug.assert_called_once_with("MCU ACK received", command_id="0x02")


@pytest.mark.asyncio
async def test_on_mcu_ack_corrupt_bytes(
    svc: tuple[BridgeService, RuntimeState, AsyncMock],
) -> None:
    service, _state, _serial = svc
    with patch("mcubridge.services.runtime.logger.error") as mock_err:
        await service._on_mcu_ack(1, b"\xff\xff\xff\xff\xff")
        mock_err.assert_called_once()


# ---------------------------------------------------------------------------
# _handle_mcu_status
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_handle_mcu_status_ok_no_payload(
    svc: tuple[BridgeService, RuntimeState, AsyncMock],
) -> None:
    service, _state, _serial = svc
    captured: list[object] = []

    async def _cap(msg: object, *, reply_context: object = None) -> None:
        captured.append(msg)

    with patch.object(service, "enqueue_cloud", side_effect=_cap):
        await service._handle_mcu_status(Status.OK, 1, b"")
    assert captured


@pytest.mark.asyncio
async def test_handle_mcu_status_error_with_generic_response(
    svc: tuple[BridgeService, RuntimeState, AsyncMock],
) -> None:
    service, _state, _serial = svc
    captured: list[object] = []

    async def _cap(msg: object, *, reply_context: object = None) -> None:
        captured.append(msg)

    payload = pb.GenericResponse(message="something failed").SerializeToString()
    with patch.object(service, "enqueue_cloud", side_effect=_cap):
        await service._handle_mcu_status(Status.ERROR, 1, payload)
    assert captured


@pytest.mark.asyncio
async def test_handle_mcu_status_error_with_protobuf_message(
    svc: tuple[BridgeService, RuntimeState, AsyncMock],
) -> None:
    service, _state, _serial = svc
    captured: list[object] = []

    async def _cap(msg: object, *, reply_context: object = None) -> None:
        captured.append(msg)

    payload = pb.GenericResponse(message="proto_msg")
    with patch.object(service, "enqueue_cloud", side_effect=_cap):
        await service._handle_mcu_status(Status.ERROR, 1, payload)
    assert captured


@pytest.mark.asyncio
async def test_handle_mcu_status_with_hex_payload(
    svc: tuple[BridgeService, RuntimeState, AsyncMock],
) -> None:
    service, _state, _serial = svc
    captured: list[object] = []

    async def _cap(msg: object, *, reply_context: object = None) -> None:
        captured.append(msg)

    with patch.object(service, "enqueue_cloud", side_effect=_cap):
        # Raw non-UTF8 bytes → hex fallback
        await service._handle_mcu_status(Status.ERROR, 1, b"\x80\x81\x82")
    assert captured


# ---------------------------------------------------------------------------
# _on_mcu_digital_read_resp / _on_mcu_analog_read_resp
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_on_mcu_digital_read_resp(
    svc: tuple[BridgeService, RuntimeState, AsyncMock],
) -> None:
    service, state, _serial = svc
    state.pending_digital_reads.append(PendingPinRequest(pin=13, reply_context=None))
    captured: list[object] = []

    async def _cap(msg: object, *, reply_context: object = None) -> None:
        captured.append(msg)

    with patch.object(service, "enqueue_cloud", side_effect=_cap):
        p = pb.DigitalReadResponse(value=1)
        await service._on_mcu_digital_read_resp(1, p)
    assert captured


@pytest.mark.asyncio
async def test_on_mcu_analog_read_resp(
    svc: tuple[BridgeService, RuntimeState, AsyncMock],
) -> None:
    service, state, _serial = svc
    state.pending_analog_reads.append(PendingPinRequest(pin=0, reply_context=None))
    captured: list[object] = []

    async def _cap(msg: object, *, reply_context: object = None) -> None:
        captured.append(msg)

    with patch.object(service, "enqueue_cloud", side_effect=_cap):
        p = pb.AnalogReadResponse(value=512)
        await service._on_mcu_analog_read_resp(1, p)
    assert captured


# ---------------------------------------------------------------------------
# _on_mcu_spi_resp
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_on_mcu_spi_resp(
    svc: tuple[BridgeService, RuntimeState, AsyncMock],
) -> None:
    service, _state, _serial = svc
    captured: list[object] = []

    async def _cap(msg: object, *, reply_context: object = None) -> None:
        captured.append(msg)

    with patch.object(service, "enqueue_cloud", side_effect=_cap):
        p = pb.SpiTransferResponse(data=b"\xaa\xbb")
        await service._on_mcu_spi_resp(1, p)
    assert captured


# ---------------------------------------------------------------------------
# _on_mcu_process_run_async
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_on_mcu_process_run_async_no_serial(
    svc: tuple[BridgeService, RuntimeState, AsyncMock],
) -> None:
    service, _state, _serial = svc
    service.serial = None  # type: ignore[assignment]
    p = pb.ProcessRunAsync(command="echo hi")
    result = await service._on_mcu_process_run_async(1, p)
    assert result is False


@pytest.mark.asyncio
async def test_on_mcu_process_run_async_disallowed(
    svc: tuple[BridgeService, RuntimeState, AsyncMock],
) -> None:
    service, _state, serial = svc
    serial.send.return_value = True
    p = pb.ProcessRunAsync(command="rm -rf /")  # not in allowed_commands
    result = await service._on_mcu_process_run_async(1, p)
    assert result is False
    args = serial.send.call_args[0]
    assert args[0] == Status.ERROR.value


@pytest.mark.asyncio
async def test_on_mcu_process_run_async_allowed(
    svc: tuple[BridgeService, RuntimeState, AsyncMock],
) -> None:
    service, _state, serial = svc
    serial.send.return_value = True
    p = pb.ProcessRunAsync(command="echo hello")
    with patch.object(service, "_run_process", new=AsyncMock(return_value=1234)):
        result = await service._on_mcu_process_run_async(1, p)
    assert result is True
    args = serial.send.call_args[0]
    assert args[0] == Command.CMD_PROCESS_RUN_ASYNC_RESP.value


@pytest.mark.asyncio
async def test_on_mcu_process_run_async_pid_zero(
    svc: tuple[BridgeService, RuntimeState, AsyncMock],
) -> None:
    service, _state, serial = svc
    serial.send.return_value = True
    p = pb.ProcessRunAsync(command="echo hello")
    with patch.object(service, "_run_process", new=AsyncMock(return_value=0)):
        result = await service._on_mcu_process_run_async(1, p)
    assert result is False


# ---------------------------------------------------------------------------
# _on_mcu_process_poll
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_on_mcu_process_poll_no_serial(
    svc: tuple[BridgeService, RuntimeState, AsyncMock],
) -> None:
    service, _state, _serial = svc
    service.serial = None  # type: ignore[assignment]
    p = pb.ProcessPoll(pid=999)
    result = await service._on_mcu_process_poll(1, p)
    assert result is False


@pytest.mark.asyncio
async def test_on_mcu_process_poll_with_result(
    svc: tuple[BridgeService, RuntimeState, AsyncMock],
) -> None:
    service, _state, serial = svc
    serial.send.return_value = True
    mock_batch = pb.ProcessPollResponse(finished=True, exit_code=0)
    p = pb.ProcessPoll(pid=42)
    with patch.object(service, "_poll_process", new=AsyncMock(return_value=mock_batch)):
        result = await service._on_mcu_process_poll(1, p)
    assert result is True
