import asyncio
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch
from pathlib import Path

import pytest
import msgspec
from aiomqtt.message import Message

# pyright: reportPrivateUsage=false
from mcubridge.services.runtime import BridgeService
from mcubridge.state.context import create_runtime_state, RuntimeState
from mcubridge.config.settings import RuntimeConfig
from mcubridge.protocol.protocol import (
    Command,
    Topic,
    ConsoleAction,
    DatastoreAction,
    MailboxAction,
    DigitalAction,
    Status,
    ShellAction,
    FileAction,
    SpiAction,
    SystemAction,
    AnalogAction,
)
from mcubridge.protocol.structures import (
    TopicRoute,
    AckPacket,
    ConsoleWritePacket,
    DatastorePutPacket,
    DatastoreGetPacket,
    MailboxPushPacket,
    FileWritePacket,
    FileReadPacket,
    FileRemovePacket,
    DigitalWritePacket,
    PinModePacket,
    AnalogWritePacket,
    ProcessRunAsyncPacket,
    ProcessKillPacket,
    ProcessPollPacket,
    DigitalReadResponsePacket,
    AnalogReadResponsePacket,
    PendingPinRequest,
    SpiTransferPacket,
    PinReadPacket,
    SpiTransferResponsePacket,
    SpiConfigPacket,
    VersionResponsePacket,
    FreeMemoryResponsePacket,
)


@pytest.fixture
def service_setup(tmp_path: Path) -> tuple[BridgeService, RuntimeState, AsyncMock, AsyncMock]:
    config = RuntimeConfig(
        mqtt_topic="br", 
        serial_port="/dev/test", 
        file_system_root=str(tmp_path),
        allowed_commands=["ls", "test_cmd"]
    )
    state = create_runtime_state(config)
    serial = AsyncMock()
    # Mock serial functions used by runtime
    serial.send = AsyncMock(return_value=True)
    serial.acknowledge = AsyncMock(return_value=True)
    serial.send_and_wait_payload = AsyncMock(return_value=None)
    serial.reset = AsyncMock()
    
    mqtt = AsyncMock()
    mqtt.enqueue_mqtt = AsyncMock()
    
    service = BridgeService(config, state, serial, mqtt)
    service.register_serial_sender(serial.send)
    return service, state, serial, mqtt


@pytest.mark.asyncio
async def test_mcu_handlers_exhaustive_v2(service_setup: Any) -> None:
    service, state, serial, mqtt = service_setup
    state.mark_synchronized()

    # 1. Test every command with valid payload
    cases = [
        (Command.CMD_XOFF.value, b""),
        (Command.CMD_XON.value, b""),
        (Command.CMD_CONSOLE_WRITE.value, msgspec.msgpack.encode(ConsoleWritePacket(data=b"hello"))),
        (Command.CMD_DATASTORE_PUT.value, msgspec.msgpack.encode(DatastorePutPacket(key="k", value=b"v"))),
        (Command.CMD_DATASTORE_GET.value, msgspec.msgpack.encode(DatastoreGetPacket(key="k"))),
        (Command.CMD_MAILBOX_PUSH.value, msgspec.msgpack.encode(MailboxPushPacket(data=b"m"))),
        (Command.CMD_MAILBOX_READ.value, b""),
        (Command.CMD_MAILBOX_AVAILABLE.value, b""),
        (Command.CMD_FILE_WRITE.value, msgspec.msgpack.encode(FileWritePacket(path="t.txt", data=b"d"))),
        (Command.CMD_FILE_READ.value, msgspec.msgpack.encode(FileReadPacket(path="t.txt"))),
        (Command.CMD_FILE_REMOVE.value, msgspec.msgpack.encode(FileRemovePacket(path="t.txt"))),
        (Command.CMD_DIGITAL_WRITE.value, msgspec.msgpack.encode(DigitalWritePacket(pin=1, value=1))),
        (Command.CMD_ANALOG_WRITE.value, msgspec.msgpack.encode(AnalogWritePacket(pin=1, value=1))),
        (Command.CMD_SET_PIN_MODE.value, msgspec.msgpack.encode(PinModePacket(pin=1, mode=1))),
        (Command.CMD_DIGITAL_READ.value, msgspec.msgpack.encode(PinReadPacket(pin=1))),
        (Command.CMD_ANALOG_READ.value, msgspec.msgpack.encode(PinReadPacket(pin=1))),
    ]
    
    for cmd, payload in cases:
        await service.handle_mcu_frame(cmd, 1, payload)

    # 2. Test Responses (Need pending futures)
    state.pending_digital_reads.append(PendingPinRequest(pin=1, reply_context=asyncio.Future()))
    await service.handle_mcu_frame(Command.CMD_DIGITAL_READ_RESP.value, 1, msgspec.msgpack.encode(DigitalReadResponsePacket(value=1)))
    
    state.pending_analog_reads.append(PendingPinRequest(pin=1, reply_context=asyncio.Future()))
    await service.handle_mcu_frame(Command.CMD_ANALOG_READ_RESP.value, 1, msgspec.msgpack.encode(AnalogReadResponsePacket(value=1)))
    
    await service.handle_mcu_frame(Command.CMD_SPI_TRANSFER_RESP.value, 1, b"\x81\xa4data\xa4resp")


@pytest.mark.asyncio
async def test_mqtt_handlers_exhaustive_v2(service_setup: Any) -> None:
    service, state, serial, _ = service_setup
    state.mark_synchronized()

    # 1. Test System Actions
    actions = [SystemAction.VERSION, SystemAction.FREE_MEMORY, SystemAction.BOOTLOADER]
    for act in actions:
        msg = Message(topic=f"br/sys/get/{act.value}", payload=b"", qos=0, retain=False, mid=1, properties=None)
        if act == SystemAction.VERSION:
             serial.send_and_wait_payload.return_value = msgspec.msgpack.encode(VersionResponsePacket(major=1, minor=0, patch=0))
        elif act == SystemAction.FREE_MEMORY:
             serial.send_and_wait_payload.return_value = msgspec.msgpack.encode(FreeMemoryResponsePacket(value=1024))
        await service.handle_mqtt_message(msg)

    # 2. Test Shell Actions
    for act in [ShellAction.RUN_ASYNC, ShellAction.POLL, ShellAction.KILL]:
        msg = Message(topic=f"br/sh/{act.value}", payload=b"ls", qos=0, retain=False, mid=1, properties=None)
        with patch("asyncio.create_subprocess_exec", AsyncMock(return_value=AsyncMock(pid=123, wait=AsyncMock(return_value=0)))):
            with patch("psutil.Process", return_value=MagicMock()):
                await service.handle_mqtt_message(msg)

    # 3. Test SPI Actions
    for act in [SpiAction.BEGIN, SpiAction.END, SpiAction.CONFIG, SpiAction.TRANSFER]:
        payload = b""
        if act == SpiAction.CONFIG:
            payload = msgspec.json.encode({"frequency": 1000, "bit_order": 0, "data_mode": 0})
        elif act == SpiAction.TRANSFER:
            payload = msgspec.msgpack.encode(SpiTransferPacket(data=b"d"))
            serial.send_and_wait_payload.return_value = msgspec.msgpack.encode(SpiTransferResponsePacket(data=b"r"))
            
        msg = Message(topic=f"br/spi/{act.value}", payload=payload, qos=0, retain=False, mid=1, properties=None)
        await service.handle_mqtt_message(msg)

    # 4. Test File Actions
    for act in [FileAction.READ, FileAction.REMOVE]:
        msg = Message(topic=f"br/file/{act.value}/t.txt", payload=b"", qos=0, retain=False, mid=1, properties=None)
        await service.handle_mqtt_message(msg)
