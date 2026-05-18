"""Massive stress and edge-case testing for McuBridge runtime service."""

from __future__ import annotations

import asyncio
import contextlib
from typing import Any, Tuple, cast
from unittest.mock import AsyncMock, patch

import msgspec
import pytest
import pytest_asyncio
from aiomqtt.message import Message

from mcubridge.config.settings import RuntimeConfig
from mcubridge.protocol.protocol import Command, Status
from mcubridge.protocol.structures import (
    AckPacket,
    AnalogReadResponsePacket,
    ConsoleWritePacket,
    DatastoreGetPacket,
    DatastorePutPacket,
    DigitalReadResponsePacket,
    FileReadPacket,
    FileReadResponsePacket,
    FileRemovePacket,
    FileWritePacket,
    MailboxPushPacket,
    PinReadPacket,
    ProcessKillPacket,
    ProcessPollPacket,
    ProcessRunAsyncPacket,
    SpiTransferResponsePacket,
)
from mcubridge.services.runtime import BridgeService
from mcubridge.state.context import RuntimeState


@pytest_asyncio.fixture
async def service_setup(
    runtime_config: RuntimeConfig, runtime_state: RuntimeState
) -> Tuple[BridgeService, RuntimeState, AsyncMock]:
    """Provide a BridgeService instance with mocked serial and MQTT."""
    serial = AsyncMock()
    # Ensure acknowledge is also an AsyncMock
    serial.acknowledge = AsyncMock()
    service = BridgeService(runtime_config, runtime_state, serial)
    mock_mqtt = AsyncMock()
    service.set_mqtt_client(mock_mqtt)
    return service, runtime_state, serial


@pytest.mark.asyncio
async def test_runtime_brute_force_handlers(
    service_setup: Tuple[BridgeService, RuntimeState, AsyncMock],
) -> None:
    """Brute-force all MCU handlers with valid and invalid payloads."""
    service, state, serial = service_setup
    state.mark_synchronized()

    handlers: list[tuple[int, int, bytes]] = [
        (Command.CMD_XON.value, 1, b""),
        (Command.CMD_XOFF.value, 1, b""),
        (Status.ACK.value, 1, msgspec.msgpack.encode(AckPacket(command_id=1))),
        (
            Command.CMD_CONSOLE_WRITE.value,
            1,
            msgspec.msgpack.encode(ConsoleWritePacket(data=b"test")),
        ),
        (
            Command.CMD_DATASTORE_PUT.value,
            1,
            msgspec.msgpack.encode(DatastorePutPacket(key="k", value=b"v")),
        ),
        (
            Command.CMD_DATASTORE_GET.value,
            1,
            msgspec.msgpack.encode(DatastoreGetPacket(key="k")),
        ),
        (
            Command.CMD_MAILBOX_PUSH.value,
            1,
            msgspec.msgpack.encode(MailboxPushPacket(data=b"m")),
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
            msgspec.msgpack.encode(FileWritePacket(path="f", data=b"")),
        ),
        (
            Command.CMD_FILE_READ.value,
            1,
            msgspec.msgpack.encode(FileReadPacket(path="f")),
        ),
        (
            Command.CMD_FILE_REMOVE.value,
            1,
            msgspec.msgpack.encode(FileRemovePacket(path="f")),
        ),
        (
            Command.CMD_FILE_READ_RESP.value,
            1,
            msgspec.msgpack.encode(FileReadResponsePacket(content=b"abc")),
        ),
        (
            Command.CMD_PROCESS_RUN_ASYNC.value,
            1,
            msgspec.msgpack.encode(ProcessRunAsyncPacket(command="ls")),
        ),
        (
            Command.CMD_PROCESS_POLL.value,
            1,
            msgspec.msgpack.encode(ProcessPollPacket(pid=1)),
        ),
        (
            Command.CMD_PROCESS_KILL.value,
            1,
            msgspec.msgpack.encode(ProcessKillPacket(pid=1)),
        ),
        (
            Command.CMD_DIGITAL_READ.value,
            1,
            msgspec.msgpack.encode(PinReadPacket(pin=1)),
        ),
        (
            Command.CMD_ANALOG_READ.value,
            1,
            msgspec.msgpack.encode(PinReadPacket(pin=1)),
        ),
        (
            Command.CMD_DIGITAL_READ_RESP.value,
            1,
            msgspec.msgpack.encode(DigitalReadResponsePacket(value=1)),
        ),
        (
            Command.CMD_ANALOG_READ_RESP.value,
            1,
            msgspec.msgpack.encode(AnalogReadResponsePacket(value=1)),
        ),
        (
            Command.CMD_SPI_TRANSFER_RESP.value,
            1,
            msgspec.msgpack.encode(SpiTransferResponsePacket(data=b"r")),
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
    service_setup: Tuple[BridgeService, RuntimeState, AsyncMock],
) -> None:
    """Test MQTT dispatcher with various topics and payloads."""
    service, state, _ = service_setup
    state.mark_synchronized()

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
        ("br/spi/config", b'{"frequency":1000000}'),
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
    service_setup: Tuple[BridgeService, RuntimeState, AsyncMock],
) -> None:
    """Test process management handles corner cases like rapid spawn/kill."""
    service, _, _ = service_setup
    svc = cast(Any, service)

    with patch("asyncio.create_subprocess_exec") as mock_exec:
        mock_proc = AsyncMock()
        mock_proc.pid = 1234
        mock_proc.returncode = None
        mock_exec.return_value = mock_proc

        # Spawn multiple
        pids = []
        for _ in range(3):
            pid = await svc._run_process("ls")
            if pid:
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
    service_setup: Tuple[BridgeService, RuntimeState, AsyncMock],
) -> None:
    """Test file operations handle OS permission errors."""
    service, _, serial = service_setup

    with patch("builtins.open") as mock_open:
        mock_open.side_effect = PermissionError("EACCES")

        # Test read
        await service.handle_mcu_frame(Command.CMD_FILE_READ.value, 1, b"\x81\xa4path\xa4test")
        # Ensure it sent an ERROR status
        assert serial.send.called
        args, _ = serial.send.call_args
        assert args[0] == Status.ERROR.value


@pytest.mark.asyncio
async def test_runtime_mcu_special_logic(
    service_setup: Tuple[BridgeService, RuntimeState, AsyncMock],
) -> None:
    """Test asynchronous race conditions and state locks in runtime service."""
    service, state, _ = service_setup
    svc = cast(Any, service)
    state.mark_synchronized()

    state.console_to_mcu_queue.append(b"pending")
    await svc._flush_console_queue()

    async with svc._storage_lock:
        # This task will block until we release the lock
        task = asyncio.create_task(svc._on_mcu_file_write(FileWritePacket(path="t", data=b"")))
        await asyncio.sleep(0.01)
        assert not task.done()

    await task
    assert task.done()
