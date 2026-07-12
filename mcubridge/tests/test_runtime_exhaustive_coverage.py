import asyncio
import sys
import pytest
from unittest.mock import AsyncMock, MagicMock, patch
from pathlib import Path

from mcubridge.protocol import mcubridge_pb2 as pb
from mcubridge.services.runtime import BridgeService, BridgeRequest
from mcubridge.transport.serial import SerialTransport
from mcubridge.state.context import create_runtime_state
from mcubridge.config.settings import RuntimeConfig

# Mock 'uci' globally
sys.modules["uci"] = MagicMock()


@pytest.fixture
def service_setup(tmp_path: Path):
    config = RuntimeConfig(
        topic_prefix="br",
        serial_port="/dev/test",
        file_system_root=str(tmp_path),
        cloud_enabled=True,
        cloud_spool_dir=str(tmp_path / "spool"),
    )
    state = create_runtime_state(config)
    state.link_sync_event.set()  # Don't block on synchronization
    serial = AsyncMock(spec=SerialTransport)
    service = BridgeService(config, state, serial)
    return service, state, serial


@pytest.mark.asyncio
async def test_mcu_file_operations_success(service_setup) -> None:
    service, state, serial = service_setup

    # Mock success path of MCU file read
    async def mock_send_raw(*args, **kwargs):
        await asyncio.sleep(0.01)
        if service._pending_mcu_read:
            service._pending_mcu_read.future.set_result(b"mcu_file_data")
        return True

    serial.send_raw.side_effect = mock_send_raw

    # Send read request
    inbound = BridgeRequest(topic="br/file/read/mcu/test.txt", payload=b"")
    await service.handle_request(inbound)
    assert serial.send_raw.called


@pytest.mark.asyncio
async def test_mcu_file_operations_fail_and_timeout(service_setup) -> None:
    service, state, serial = service_setup

    # 1. Send raw fails
    serial.send_raw.side_effect = None
    serial.send_raw.return_value = False
    inbound = BridgeRequest(topic="br/file/read/mcu/test.txt", payload=b"")
    await service.handle_request(inbound)

    # 2. Timeout
    serial.send_raw.return_value = True
    state.serial_response_timeout_ms = 10
    inbound = BridgeRequest(topic="br/file/read/mcu/test.txt", payload=b"")
    await service.handle_request(inbound)


@pytest.mark.asyncio
async def test_mcu_file_write_and_remove(service_setup) -> None:
    service, state, serial = service_setup

    # Write
    serial.send.return_value = True
    inbound = BridgeRequest(topic="br/file/write/mcu/test.txt", payload=b"data")
    await service.handle_request(inbound)

    # Remove
    inbound = BridgeRequest(topic="br/file/remove/mcu/test.txt", payload=b"")
    await service.handle_request(inbound)


@pytest.mark.asyncio
async def test_shell_operations_run_async(service_setup) -> None:
    service, state, serial = service_setup

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
        inbound = BridgeRequest(topic="br/shell/run_async", payload=b"echo hello")
        await service.handle_request(inbound)

        # Run protobuf command
        pb_req = pb.ProcessRunAsync(command="echo pb")
        inbound = BridgeRequest(
            topic="br/shell/run_async",
            payload=pb_req.SerializeToString(),
        )
        inbound.content_type = "application/x-protobuf"
        await service.handle_request(inbound)


@pytest.mark.asyncio
async def test_shell_operations_poll_and_kill(service_setup) -> None:
    service, state, serial = service_setup

    # Test polling with invalid PID
    inbound = BridgeRequest(topic="br/shell/poll/1234", payload=b"")
    await service.handle_request(inbound)

    # Test killing with invalid PID
    inbound = BridgeRequest(topic="br/shell/kill/1234", payload=b"")
    await service.handle_request(inbound)


@pytest.mark.asyncio
async def test_gpio_pin_handlers(service_setup) -> None:
    service, state, serial = service_setup

    # Digital read
    serial.send.return_value = True
    inbound = BridgeRequest(topic="br/digital/read/13", payload=b"")
    # Run in background to set future
    task = asyncio.create_task(service.handle_request(inbound))
    await asyncio.sleep(0.01)
    if state.pending_digital_reads:
        state.pending_digital_reads[0].reply_context.set_result(1)
    await task

    # Digital write
    inbound = BridgeRequest(topic="br/digital/write/13", payload=b"1")
    await service.handle_request(inbound)

    # Analog read
    inbound = BridgeRequest(topic="br/analog/read/2", payload=b"")
    task = asyncio.create_task(service.handle_request(inbound))
    await asyncio.sleep(0.01)
    if state.pending_analog_reads:
        state.pending_analog_reads[0].reply_context.set_result(512)
    await task

    # Analog write
    inbound = BridgeRequest(topic="br/analog/write/3", payload=b"255")
    await service.handle_request(inbound)


@pytest.mark.asyncio
async def test_spi_operations(service_setup) -> None:
    service, state, serial = service_setup

    # SPI Begin
    inbound = BridgeRequest(topic="br/spi/begin", payload=b"")
    await service.handle_request(inbound)

    # SPI End
    inbound = BridgeRequest(topic="br/spi/end", payload=b"")
    await service.handle_request(inbound)

    # SPI Transfer
    serial.send.return_value = pb.SpiTransferResponse(data=b"world").SerializeToString()
    inbound = BridgeRequest(topic="br/spi/transfer", payload=b"hello")
    await service.handle_request(inbound)
