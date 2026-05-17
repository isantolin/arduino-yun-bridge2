import asyncio
import contextlib
from pathlib import Path
from typing import Generator, Tuple, Type
from unittest.mock import AsyncMock, MagicMock, patch

import msgspec
import pytest
from aiomqtt.message import Message

# pyright: reportPrivateUsage=false
from mcubridge.config.settings import RuntimeConfig
from mcubridge.protocol.protocol import Command, Topic
from mcubridge.protocol.structures import (
    AckPacket,
    AnalogReadResponsePacket,
    ConsoleWritePacket,
    DatastoreGetPacket,
    DatastoreGetResponsePacket,
    DatastorePutPacket,
    DigitalReadResponsePacket,
    FileReadPacket,
    FileReadResponsePacket,
    FileRemovePacket,
    FileWritePacket,
    MailboxProcessedPacket,
    MailboxPushPacket,
    PinReadPacket,
    ProcessKillPacket,
    ProcessPollPacket,
    ProcessRunAsyncPacket,
    SpiTransferResponsePacket,
    TopicRoute,
)
from mcubridge.services.runtime import BridgeService
from mcubridge.state.context import RuntimeState, create_runtime_state


@pytest.fixture
def service_setup(
    tmp_path: Path,
) -> Generator[Tuple[BridgeService, RuntimeState, AsyncMock], None, None]:
    config = RuntimeConfig(
        mqtt_topic="br",
        serial_port="/dev/test",
        file_system_root=str(tmp_path),
        allowed_commands=["ls"],
        serial_shared_secret=b"secure_secret_1234567890123456",
    )
    state = create_runtime_state(config)
    serial = AsyncMock()
    # Mock default behavior for send_and_wait_payload to avoid None issues
    serial.send_and_wait_payload.return_value = None
    service = BridgeService(config, state, serial)
    # Register mock sender
    service.register_serial_sender(serial.send)

    yield service, state, serial

    # [SIL-2] Ensure all processes and tasks are terminated to prevent ResourceWarnings
    state.cleanup()
    with contextlib.suppress(RuntimeError):
        loop = asyncio.get_running_loop()
        if loop.is_running():
            for task in asyncio.all_tasks(loop):
                if "_monitor_process" in str(task):
                    task.cancel()


@pytest.mark.asyncio
async def test_runtime_brute_force_handlers(
    service_setup: Tuple[BridgeService, RuntimeState, AsyncMock],
) -> None:
    """Brute-force all MCU handlers with valid and invalid payloads."""
    service, state, serial = service_setup
    state.mark_synchronized()

    handlers = [
        (service._handle_mcu_xon, 1, b""),
        (service._handle_mcu_xoff, 1, b""),
        (service._handle_mcu_ack, 1, msgspec.msgpack.encode(AckPacket(command_id=1))),
        (
            service._handle_mcu_console_write,
            1,
            msgspec.msgpack.encode(ConsoleWritePacket(data=b"test")),
        ),
        (
            service._handle_mcu_datastore_put,
            1,
            msgspec.msgpack.encode(DatastorePutPacket(key="k", value=b"v")),
        ),
        (
            service._handle_mcu_datastore_get,
            1,
            msgspec.msgpack.encode(DatastoreGetPacket(key="k")),
        ),
        (
            service._handle_mcu_mailbox_push,
            1,
            msgspec.msgpack.encode(MailboxPushPacket(data=b"m")),
        ),
        (service._handle_mcu_mailbox_read, 1, b""),
        (service._handle_mcu_mailbox_available, 1, b""),
        (
            service._handle_mcu_mailbox_processed,
            1,
            msgspec.msgpack.encode(MailboxProcessedPacket(message_id=1)),
        ),
        (
            service._handle_mcu_file_write,
            1,
            msgspec.msgpack.encode(FileWritePacket(path="f", data=b"")),
        ),
        (
            service._handle_mcu_file_read,
            1,
            msgspec.msgpack.encode(FileReadPacket(path="f")),
        ),
        (
            service._handle_mcu_file_remove,
            1,
            msgspec.msgpack.encode(FileRemovePacket(path="f")),
        ),
        (
            service._handle_mcu_file_read_resp,
            1,
            msgspec.msgpack.encode(FileReadResponsePacket(content=b"abc")),
        ),
        (
            service._handle_mcu_process_run,
            1,
            msgspec.msgpack.encode(ProcessRunAsyncPacket(command="ls")),
        ),
        (
            service._handle_mcu_process_poll,
            1,
            msgspec.msgpack.encode(ProcessPollPacket(pid=1)),
        ),
        (
            service._handle_mcu_process_kill,
            1,
            msgspec.msgpack.encode(ProcessKillPacket(pid=1)),
        ),
        (
            service._handle_mcu_pin_digital_read,
            1,
            msgspec.msgpack.encode(PinReadPacket(pin=1)),
        ),
        (
            service._handle_mcu_pin_analog_read,
            1,
            msgspec.msgpack.encode(PinReadPacket(pin=1)),
        ),
        (
            service._handle_mcu_pin_digital_read_resp,
            1,
            msgspec.msgpack.encode(DigitalReadResponsePacket(value=1)),
        ),
        (
            service._handle_mcu_pin_analog_read_resp,
            1,
            msgspec.msgpack.encode(AnalogReadResponsePacket(value=1)),
        ),
        (
            service._handle_mcu_spi_resp,
            1,
            msgspec.msgpack.encode(SpiTransferResponsePacket(data=b"r")),
        ),
    ]

    for handler, seq, payload in handlers:
        serial.send.reset_mock()
        service.enqueue_mqtt = AsyncMock()

        # Test valid payload
        with patch("asyncio.create_subprocess_exec") as mock_exec:
            mock_exec.return_value = AsyncMock()
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
    """Brute-force all MQTT handlers with valid and invalid payloads."""
    service, state, _ = service_setup
    state.mark_synchronized()

    msg = Message(topic="br/test", payload=b"{}", qos=0, retain=False, mid=1, properties=None)
    route = TopicRoute(raw="br/test", prefix="br", topic=Topic.SYSTEM, segments=("get", "version"))

    mqtt_handlers = [
        (service._handle_mqtt_console, msg),
        (service._handle_mqtt_datastore, route, msg),
        (service._handle_mqtt_mailbox, route, msg),
        (service._handle_mqtt_file, route, msg),
        (service._handle_mqtt_shell, route, msg),
        (service._handle_mqtt_spi, route, msg),
        (service._handle_mqtt_pin, route, msg),
        (service._handle_mqtt_system, route, msg),
    ]

    for entry in mqtt_handlers:
        handler = entry[0]
        args = entry[1:]
        with contextlib.suppress(asyncio.CancelledError, OSError, ValueError):
            await handler(*args)  # type: ignore


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "cmd_id, packet_cls",
    [
        (Command.CMD_DIGITAL_READ_RESP.value, DigitalReadResponsePacket),
        (Command.CMD_ANALOG_READ_RESP.value, AnalogReadResponsePacket),
        (Command.CMD_DATASTORE_GET_RESP.value, DatastoreGetResponsePacket),
        (Command.CMD_SPI_TRANSFER_RESP.value, SpiTransferResponsePacket),
        (Command.CMD_FILE_READ.value, FileReadPacket),
    ],
)
async def test_runtime_mcu_frame_fuzzing(
    service_setup: Tuple[BridgeService, RuntimeState, AsyncMock],
    cmd_id: int,
    packet_cls: Type[msgspec.Struct],
) -> None:
    """Ensure MCU frame handlers don't crash on corrupt/unexpected MsgPack."""
    service, state, _ = service_setup
    state.mark_synchronized()

    # 1. Empty payload
    await service.handle_mcu_frame(cmd_id, 1, b"")

    # 2. Random bytes
    await service.handle_mcu_frame(cmd_id, 1, b"\xde\xad\xbe\xef")

    # 3. Wrong MsgPack type (e.g. integer instead of map)
    await service.handle_mcu_frame(cmd_id, 1, b"\x01")


@pytest.mark.asyncio
async def test_runtime_process_cleanup_robustness(
    service_setup: Tuple[BridgeService, RuntimeState, AsyncMock],
) -> None:
    """Test process finalization handles race conditions where PID disappears."""
    service, state, _ = service_setup

    # Mock a running process
    mock_proc = MagicMock()
    mock_proc.pid = 999999
    mock_proc.returncode = None
    state.running_processes[123] = mock_proc

    result = await service._stop_process(123)
    assert result is True
    assert 123 not in state.running_processes


@pytest.mark.asyncio
async def test_runtime_file_ops_permission_errors(
    service_setup: Tuple[BridgeService, RuntimeState, AsyncMock],
) -> None:
    """Test file operations handle OS permission errors."""
    service, _, serial = service_setup

    with patch("builtins.open") as mock_open:
        mock_open.side_effect = PermissionError("EACCES")

        # Test read
        await service._handle_mcu_file_read(1, b"\x81\xa4path\xa4test")
        assert serial.send.called

        # Test write
        payload = msgspec.msgpack.encode(FileWritePacket(path="test.txt", data=b"data"))
        await service._handle_mcu_file_write(1, payload)
        assert serial.send.called


@pytest.mark.asyncio
async def test_runtime_mcu_special_logic(
    service_setup: Tuple[BridgeService, RuntimeState, AsyncMock],
) -> None:
    """Test asynchronous race conditions and state locks in runtime service."""
    service, state, _ = service_setup
    state.mark_synchronized()

    state.console_to_mcu_queue.append(b"pending")
    await service._flush_console_queue()

    async with service._storage_lock:
        asyncio.create_task(
            service._handle_mcu_file_write(1, msgspec.msgpack.encode(FileWritePacket(path="t", data=b"")))
        )
        await asyncio.sleep(0.01)

    msg = Message(
        topic="br/spi/config",
        payload=b"invalid{json",
        qos=0,
        retain=False,
        mid=1,
        properties=None,
    )
    await service.handle_mqtt_message(msg)
