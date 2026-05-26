"""Massive stress and edge-case testing for McuBridge runtime service."""

from __future__ import annotations
from mcubridge.protocol import mcubridge_pb2 as pb

import asyncio
import contextlib
from typing import Any, cast
from unittest.mock import AsyncMock, patch

import pytest
import pytest_asyncio
import msgspec
from aiomqtt.message import Message

from mcubridge.config.settings import RuntimeConfig
from mcubridge.protocol.protocol import Command, Status
from mcubridge.protocol.structures import (
    AllowedCommandPolicy,
)
from mcubridge.services.runtime import BridgeService
from mcubridge.state.context import RuntimeState


@pytest_asyncio.fixture
async def service_setup(
    runtime_config: RuntimeConfig, runtime_state: RuntimeState
) -> tuple[BridgeService, RuntimeState, AsyncMock]:
    """Provide a BridgeService instance with mocked serial and MQTT."""
    serial = AsyncMock()
    serial.send = AsyncMock(return_value=True)
    serial.acknowledge = AsyncMock(return_value=True)
    serial.send = AsyncMock(return_value=None)
    serial.reset = AsyncMock(return_value=None)
    service = BridgeService(runtime_config, runtime_state, serial)
    mock_mqtt = AsyncMock()
    service.set_mqtt_client(mock_mqtt)
    return service, runtime_state, serial


@pytest.mark.asyncio
async def test_runtime_brute_force_handlers(
    service_setup: tuple[BridgeService, RuntimeState, AsyncMock],
) -> None:
    """Brute-force all MCU handlers with valid and invalid payloads."""
    service, state, serial = service_setup
    state.mark_synchronized()

    handlers: list[tuple[int, int, bytes]] = [
        (Command.CMD_XON.value, 1, b""),
        (Command.CMD_XOFF.value, 1, b""),
        (Status.ACK.value, 1, pb.AckPacket(command_id=1).SerializeToString()),
        (
            Command.CMD_CONSOLE_WRITE.value,
            1,
            pb.ConsoleWrite(data=b"test").SerializeToString(),
        ),
        (
            Command.CMD_DATASTORE_PUT.value,
            1,
            pb.DatastorePut(key="k", value=b"v").SerializeToString(),
        ),
        (
            Command.CMD_DATASTORE_GET.value,
            1,
            pb.DatastoreGet(key="k").SerializeToString(),
        ),
        (
            Command.CMD_MAILBOX_PUSH.value,
            1,
            pb.MailboxPush(data=b"m").SerializeToString(),
        ),
        (Command.CMD_MAILBOX_READ.value, 1, b""),
        (Command.CMD_MAILBOX_AVAILABLE.value, 1, b""),
        (
            Command.CMD_MAILBOX_PROCESSED.value,
            1,
            b"processed_payload",
        ),
        (
            Command.CMD_FILE_WRITE.value,
            1,
            pb.FileWrite(path="f", data=b"").SerializeToString(),
        ),
        (
            Command.CMD_FILE_READ.value,
            1,
            pb.FileRead(path="f").SerializeToString(),
        ),
        (
            Command.CMD_FILE_REMOVE.value,
            1,
            pb.FileRemove(path="f").SerializeToString(),
        ),
        (
            Command.CMD_FILE_READ_RESP.value,
            1,
            pb.FileReadResponse(content=b"abc").SerializeToString(),
        ),
        (
            Command.CMD_PROCESS_RUN_ASYNC.value,
            1,
            pb.ProcessRunAsync(command="ls").SerializeToString(),
        ),
        (
            Command.CMD_PROCESS_POLL.value,
            1,
            pb.ProcessPoll(pid=1).SerializeToString(),
        ),
        (
            Command.CMD_PROCESS_KILL.value,
            1,
            pb.ProcessKill(pid=1).SerializeToString(),
        ),
        (
            Command.CMD_DIGITAL_READ.value,
            1,
            pb.PinRead(pin=1).SerializeToString(),
        ),
        (
            Command.CMD_ANALOG_READ.value,
            1,
            pb.PinRead(pin=1).SerializeToString(),
        ),
        (
            Command.CMD_DIGITAL_READ_RESP.value,
            1,
            pb.DigitalReadResponse(value=1).SerializeToString(),
        ),
        (
            Command.CMD_ANALOG_READ_RESP.value,
            1,
            pb.AnalogReadResponse(value=1).SerializeToString(),
        ),
        (
            Command.CMD_SPI_TRANSFER_RESP.value,
            1,
            pb.SpiTransferResponse(data=b"r").SerializeToString(),
        ),
    ]

    for cmd_id, seq, payload in handlers:
        serial.send.reset_mock()
        service.enqueue_mqtt = AsyncMock()
        handler = service.mcu_registry[cmd_id]

        # Test valid payload
        with patch("asyncio.create_subprocess_exec") as mock_exec:
            mock_proc = AsyncMock()
            mock_proc.pid = 123
            mock_exec.return_value = mock_proc
            await handler(seq, payload)

        # Test invalid payload (should not crash)
        serial.send.reset_mock()
        service.enqueue_mqtt = AsyncMock()
        with contextlib.suppress(asyncio.CancelledError, OSError, ValueError):
            await handler(seq, b"\xff")


@pytest.mark.asyncio
async def test_runtime_mqtt_brute_force(
    service_setup: tuple[BridgeService, RuntimeState, AsyncMock],
) -> None:
    """Test MQTT dispatcher with various topics and payloads."""
    service, state, serial = service_setup
    state.mark_synchronized()

    async def send_side_effect(command_id: int, payload: bytes) -> bool | bytes:
        if command_id == Command.CMD_FILE_READ.value:

            async def complete_file_read() -> None:
                file_read_handler = service.mcu_registry[Command.CMD_FILE_READ_RESP.value]
                await file_read_handler(1, pb.FileReadResponse(content=b"abc").SerializeToString())
                await file_read_handler(1, pb.FileReadResponse(content=b"").SerializeToString())

            asyncio.get_running_loop().create_task(complete_file_read())
        elif command_id == Command.CMD_GET_VERSION.value:
            return pb.VersionResponse(major=2, minor=8, patch=5).SerializeToString()
        elif command_id == Command.CMD_GET_FREE_MEMORY.value:
            return pb.FreeMemoryResponse(value=1024).SerializeToString()
        elif command_id == Command.CMD_SPI_TRANSFER.value:
            return pb.SpiTransferResponse(data=b"\xaa\xbb").SerializeToString()
        elif command_id == Command.CMD_DATASTORE_GET.value:
            return pb.DatastoreGetResponse(value=msgspec.Raw(b"cached")).SerializeToString()
        return True

    serial.send.side_effect = send_side_effect

    # Use actual protocol constants for topics
    topics = [
        ("br/console/in", b"data"),
        ("br/datastore/put/test", b"value"),
        ("br/datastore/get/test/request", b""),
        ("br/mailbox/write", b"msg"),
        ("br/mailbox/read", b""),
        ("br/file/write/test.txt", b"content"),
        ("br/file/read/test.txt", b""),
        ("br/file/remove/test.txt", b""),
        ("br/file/write/mcu/arduino.bin", b"hex"),
        ("br/file/read/mcu/arduino.bin", b""),
        ("br/file/remove/mcu/arduino.bin", b""),
        ("br/shell/run_async", b"uptime"),
        ("br/shell/poll/123", b""),
        ("br/shell/kill/123", b""),
        ("br/spi/begin", b""),
        ("br/spi/end", b""),
        ("br/spi/config", pb.SpiConfig(frequency=1000000).SerializeToString()),
        ("br/spi/transfer", b"\x01\x02"),
        ("br/digital/13", b"1"),
        ("br/digital/13/read", b""),
        ("br/digital/13/mode", b"1"),
        ("br/analog/A0", b"128"),
        ("br/analog/A0/read", b""),
        ("br/system/bootloader", b""),
        ("br/system/free_memory/get", b""),
        ("br/system/version/get", b""),
        ("br/system/bridge/summary", b""),
        ("br/system/bridge/handshake", b""),
    ]

    for topic, payload in topics:
        msg = Message(
            topic=topic,
            payload=payload,
            qos=0,
            retain=False,
            mid=1,
            properties=None,
        )
        # We don't assert side effects, just that it doesn't crash
        await service.handle_mqtt_message(msg)


@pytest.mark.asyncio
async def test_runtime_process_cleanup_robustness(
    service_setup: tuple[BridgeService, RuntimeState, AsyncMock],
) -> None:
    """Test process management handles corner cases like rapid spawn/kill."""
    service, _, _ = service_setup
    svc = cast(Any, service)

    service.state.allowed_policy = AllowedCommandPolicy(entries=("*",))
    with patch("asyncio.create_subprocess_exec") as mock_exec:
        mock_proc = AsyncMock()
        mock_proc.pid = 1234
        mock_proc.returncode = None
        mock_exec.return_value = mock_proc

        # Spawn multiple
        pids: list[int] = []
        for _ in range(3):
            pid = cast(int, await svc._run_process("ls"))
            if pid > 0:
                pids.append(pid)

        assert len(pids) > 0

        # Kill all
        for pid in pids:
            await svc._stop_process(pid)

        # Finalize multiple times
        for pid in pids:
            svc._finalize_process(pid)
            svc._finalize_process(pid)


@pytest.mark.asyncio
async def test_runtime_file_ops_permission_errors(
    service_setup: tuple[BridgeService, RuntimeState, AsyncMock],
) -> None:
    """Test file operations handle OS permission errors."""
    service, state, serial = service_setup
    state.mark_synchronized()

    await service.handle_mcu_frame(
        Command.CMD_FILE_READ.value,
        1,
        pb.FileRead(path="test").SerializeToString(),
    )
    assert serial.send.called
    args, _ = serial.send.call_args
    assert args[0] == Status.ERROR.value


@pytest.mark.asyncio
async def test_runtime_mcu_special_logic(
    service_setup: tuple[BridgeService, RuntimeState, AsyncMock],
) -> None:
    """Test asynchronous race conditions and state locks in runtime service."""
    service, state, _ = service_setup
    svc = cast(Any, service)
    state.mark_synchronized()

    state.console_to_mcu_queue.append(b"pending")
    await svc._flush_console_queue()

    async with svc._storage_lock:
        # This task will block until we release the lock
        task = asyncio.create_task(svc._on_mcu_file_write(pb.FileWrite(path="t", data=b"")))
        await asyncio.sleep(0.01)
        assert not task.done()

    await task
    assert task.done()
