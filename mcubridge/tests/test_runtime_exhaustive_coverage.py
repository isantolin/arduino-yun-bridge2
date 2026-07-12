import asyncio
import sys
import pytest
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch
from pathlib import Path
from dataclasses import dataclass

from mcubridge.protocol import mcubridge_pb2 as pb
from mcubridge.services.runtime import BridgeService, BridgeRequest
from mcubridge.transport.serial import SerialTransport
from mcubridge.state.context import create_runtime_state, RuntimeState
from mcubridge.config.settings import RuntimeConfig
from mcubridge.protocol.protocol import Command

# Mock 'uci' globally
sys.modules["uci"] = MagicMock()


@dataclass
class CustomBridgeRequest(BridgeRequest):
    content_type: str | None = None


@pytest.fixture
def service_setup(tmp_path: Path) -> tuple[BridgeService, RuntimeState, Any]:
    config = RuntimeConfig(
        topic_prefix="br",
        serial_port="/dev/test",
        file_system_root=str(tmp_path),
        cloud_enabled=True,
        cloud_spool_dir=str(tmp_path / "spool"),
    )
    state = create_runtime_state(config)
    state.link_sync_event.set()  # Don't block on synchronization
    serial: Any = AsyncMock(spec=SerialTransport)
    service = BridgeService(config, state, serial)
    return service, state, serial


@pytest.mark.asyncio
async def test_mcu_file_operations_success(service_setup: tuple[BridgeService, RuntimeState, Any]) -> None:
    service, _, serial = service_setup

    # Mock success path of MCU file read
    async def mock_send_raw(*args: Any, **kwargs: Any) -> bool:
        await asyncio.sleep(0.01)
        pending = getattr(service, "_pending_mcu_read")
        if pending is not None:
            pending.future.set_result(b"mcu_file_data")
        return True

    serial.send_raw.side_effect = mock_send_raw

    # Send read request
    inbound = CustomBridgeRequest(topic="br/file/read/mcu/test.txt", payload=b"")
    await service.handle_request(inbound)
    assert serial.send_raw.called


@pytest.mark.asyncio
async def test_mcu_file_operations_fail_and_timeout(service_setup: tuple[BridgeService, RuntimeState, Any]) -> None:
    service, state, serial = service_setup

    # 1. Send raw fails
    serial.send_raw.side_effect = None
    serial.send_raw.return_value = False
    inbound = CustomBridgeRequest(topic="br/file/read/mcu/test.txt", payload=b"")
    await service.handle_request(inbound)

    # 2. Timeout
    serial.send_raw.return_value = True
    state.serial_response_timeout_ms = 10
    inbound = CustomBridgeRequest(topic="br/file/read/mcu/test.txt", payload=b"")
    await service.handle_request(inbound)


@pytest.mark.asyncio
async def test_mcu_file_write_and_remove(service_setup: tuple[BridgeService, RuntimeState, Any]) -> None:
    service, _, serial = service_setup

    # Write
    serial.send.return_value = True
    inbound = CustomBridgeRequest(topic="br/file/write/mcu/test.txt", payload=b"data")
    await service.handle_request(inbound)

    # Remove
    inbound = CustomBridgeRequest(topic="br/file/remove/mcu/test.txt", payload=b"")
    await service.handle_request(inbound)


@pytest.mark.asyncio
async def test_shell_operations_run_async(service_setup: tuple[BridgeService, RuntimeState, Any]) -> None:
    service, _, _ = service_setup

    mock_proc = AsyncMock()
    mock_proc.pid = 9999
    mock_proc.stdout.read = AsyncMock(return_value=b"stdout_out")
    mock_proc.stdout.at_eof.side_effect = [False, True]
    mock_proc.stderr.read = AsyncMock(return_value=b"stderr_out")
    mock_proc.stderr.at_eof.side_effect = [False, True]
    mock_proc.wait = AsyncMock(return_value=0)
    mock_proc.returncode = 0

    with patch("asyncio.create_subprocess_exec", return_value=mock_proc):
        # Run text command
        inbound = CustomBridgeRequest(topic="br/shell/run_async", payload=b"echo hello")
        await service.handle_request(inbound)

        # Run protobuf command
        pb_req = pb.ProcessRunAsync(command="echo pb")
        inbound2 = CustomBridgeRequest(
            topic="br/shell/run_async",
            payload=pb_req.SerializeToString(),
            content_type="application/x-protobuf",
        )
        await service.handle_request(inbound2)


@pytest.mark.asyncio
async def test_shell_operations_poll_and_kill(service_setup: tuple[BridgeService, RuntimeState, Any]) -> None:
    service, _, _ = service_setup

    # Test polling with invalid PID
    inbound = CustomBridgeRequest(topic="br/shell/poll/1234", payload=b"")
    await service.handle_request(inbound)

    # Test killing with invalid PID
    inbound = CustomBridgeRequest(topic="br/shell/kill/1234", payload=b"")
    await service.handle_request(inbound)


@pytest.mark.asyncio
async def test_gpio_pin_handlers(service_setup: tuple[BridgeService, RuntimeState, Any]) -> None:
    service, state, serial = service_setup

    # Digital read
    serial.send.return_value = True
    inbound = CustomBridgeRequest(topic="br/digital/read/13", payload=b"")
    # Run in background to set future
    task = asyncio.create_task(service.handle_request(inbound))
    await asyncio.sleep(0.01)
    if state.pending_digital_reads:
        future = state.pending_digital_reads[0].reply_context
        if future is not None:
            future.set_result(1)
    await task

    # Digital write
    inbound = CustomBridgeRequest(topic="br/digital/write/13", payload=b"1")
    await service.handle_request(inbound)

    # Analog read
    inbound = CustomBridgeRequest(topic="br/analog/read/2", payload=b"")
    task = asyncio.create_task(service.handle_request(inbound))
    await asyncio.sleep(0.01)
    if state.pending_analog_reads:
        future = state.pending_analog_reads[0].reply_context
        if future is not None:
            future.set_result(512)
    await task

    # Analog write
    inbound = CustomBridgeRequest(topic="br/analog/write/3", payload=b"255")
    await service.handle_request(inbound)


@pytest.mark.asyncio
async def test_spi_operations(service_setup: tuple[BridgeService, RuntimeState, Any]) -> None:
    service, _, serial = service_setup

    # SPI Begin
    inbound = CustomBridgeRequest(topic="br/spi/begin", payload=b"")
    await service.handle_request(inbound)

    # SPI End
    inbound = CustomBridgeRequest(topic="br/spi/end", payload=b"")
    await service.handle_request(inbound)

    # SPI Transfer
    serial.send.return_value = pb.SpiTransferResponse(data=b"world").SerializeToString()
    inbound = CustomBridgeRequest(topic="br/spi/transfer", payload=b"hello")
    await service.handle_request(inbound)


@pytest.mark.asyncio
async def test_mcu_frame_handlers_exhaustive(service_setup: tuple[BridgeService, RuntimeState, Any]) -> None:
    service, _, serial = service_setup

    # Console Write
    await service.handle_mcu_frame(Command.CMD_CONSOLE_WRITE.value, 1, pb.ConsoleWrite(data=b"test console"))

    # Datastore Put
    await service.handle_mcu_frame(Command.CMD_DATASTORE_PUT.value, 2, pb.DatastorePut(key="mykey", value=b"myval"))

    # Datastore Get
    serial.send.return_value = True
    await service.handle_mcu_frame(Command.CMD_DATASTORE_GET.value, 3, pb.DatastoreGet(key="mykey"))

    # Mailbox Push
    await service.handle_mcu_frame(Command.CMD_MAILBOX_PUSH.value, 4, pb.MailboxPush(data=b"msg"))

    # Mailbox Available
    await service.handle_mcu_frame(Command.CMD_MAILBOX_AVAILABLE.value, 5, b"")

    # Mailbox Read
    await service.handle_mcu_frame(Command.CMD_MAILBOX_READ.value, 6, b"")

    # Mailbox Processed
    await service.handle_mcu_frame(Command.CMD_MAILBOX_PROCESSED.value, 7, pb.MailboxProcessed(message_id=1))

    # File Write
    await service.handle_mcu_frame(Command.CMD_FILE_WRITE.value, 8, pb.FileWrite(path="test.txt", data=b"content"))

    # File Remove
    await service.handle_mcu_frame(Command.CMD_FILE_REMOVE.value, 9, pb.FileRemove(path="test.txt"))

    # File Read Response
    class MockPendingMcuRead:
        def __init__(self, future: asyncio.Future[bytes]) -> None:
            self.future = future
            self.chunks: list[bytes] = []

    pending = MockPendingMcuRead(asyncio.get_running_loop().create_future())
    setattr(service, "_pending_mcu_read", pending)
    await service.handle_mcu_frame(Command.CMD_FILE_READ_RESP.value, 10, pb.FileReadResponse(content=b"chunk1"))
    await service.handle_mcu_frame(Command.CMD_FILE_READ_RESP.value, 11, pb.FileReadResponse(content=b""))


@pytest.mark.asyncio
async def test_system_operations(service_setup: tuple[BridgeService, RuntimeState, Any]) -> None:
    service, _, serial = service_setup

    # Bootloader
    inbound = CustomBridgeRequest(topic="br/system/bootloader", payload=b"")
    await service.handle_request(inbound)

    # Free Memory
    serial.send.return_value = pb.FreeMemoryResponse(value=2048).SerializeToString()
    inbound = CustomBridgeRequest(topic="br/system/free_memory/get", payload=b"")
    await service.handle_request(inbound)

    # Version
    serial.send.return_value = pb.VersionResponse(major=1, minor=0, patch=0).SerializeToString()
    inbound = CustomBridgeRequest(topic="br/system/version/get", payload=b"")
    await service.handle_request(inbound)

    # Bridge Summary
    inbound = CustomBridgeRequest(topic="br/system/bridge", payload=b"")
    await service.handle_request(inbound)

    # Bridge Handshake Snapshot
    inbound = CustomBridgeRequest(topic="br/system/bridge/handshake", payload=b"")
    await service.handle_request(inbound)


@pytest.mark.asyncio
async def test_mcu_process_run(service_setup: tuple[BridgeService, RuntimeState, Any]) -> None:
    service, _, _ = service_setup
    mock_proc = AsyncMock()
    mock_proc.pid = 8888
    mock_proc.stdout.read = AsyncMock(return_value=b"stdout")
    mock_proc.stdout.at_eof.side_effect = [False, True]
    mock_proc.stderr.read = AsyncMock(return_value=b"stderr")
    mock_proc.stderr.at_eof.side_effect = [False, True]
    mock_proc.wait = AsyncMock(return_value=0)
    mock_proc.returncode = 0

    with patch("asyncio.create_subprocess_exec", return_value=mock_proc):
        await service.handle_mcu_frame(
            Command.CMD_PROCESS_RUN_ASYNC.value,
            1,
            pb.ProcessRunAsync(command="echo hello").SerializeToString(),
        )
