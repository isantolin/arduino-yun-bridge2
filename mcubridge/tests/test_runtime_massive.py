import asyncio
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch
from pathlib import Path
import os

import pytest
import msgspec
from aiomqtt.message import Message

# pyright: reportPrivateUsage=false
from mcubridge.services.runtime import BridgeService
from mcubridge.state.context import create_runtime_state, RuntimeState
from mcubridge.config.settings import RuntimeConfig
from mcubridge.protocol.protocol import (
    Command, Topic, Status, ShellAction, FileAction, SpiAction, SystemAction, MailboxAction, AnalogAction, DigitalAction
)
from mcubridge.protocol.structures import (
    TopicRoute, AckPacket, ConsoleWritePacket, DatastorePutPacket, DatastoreGetPacket,
    MailboxPushPacket, FileWritePacket, FileReadPacket, FileRemovePacket,
    DigitalWritePacket, PinModePacket, AnalogWritePacket, PinReadPacket,
    DigitalReadResponsePacket, AnalogReadResponsePacket, SpiTransferPacket,
    SpiTransferResponsePacket, VersionResponsePacket, FreeMemoryResponsePacket,
    GenericResponsePacket, SpiConfigPacket, PendingPinRequest, MailboxProcessedPacket
)


@pytest.fixture
def service_setup(tmp_path: Path) -> tuple[BridgeService, RuntimeState, AsyncMock, AsyncMock]:
    config = RuntimeConfig(
        mqtt_topic="br", 
        serial_port="/dev/test", 
        file_system_root=str(tmp_path),
        allowed_commands=["ls"]
    )
    state = create_runtime_state(config)
    serial = AsyncMock()
    mqtt = AsyncMock()
    service = BridgeService(config, state, serial, mqtt)
    return service, state, serial, mqtt


@pytest.mark.asyncio
async def test_runtime_error_branches_v6(service_setup: Any) -> None:
    service, state, serial, mqtt = service_setup
    state.mark_synchronized()

    # 1. Malformed Packet in every handler
    for cmd_id, handler in service.mcu_registry.items():
        await handler(1, b"\xff\xff\xff") 
        
    # 2. Permission Errors in File ops
    with patch("builtins.open", side_effect=PermissionError()):
        await service._handle_mcu_file_write(1, msgspec.msgpack.encode(FileWritePacket(path="t", data=b"")))
        await service._handle_mcu_file_read(2, msgspec.msgpack.encode(FileReadPacket(path="t")))
        
    # 3. NoSuchProcess in Kill
    with patch("psutil.Process", side_effect=Exception("no proc")):
        await service._handle_mcu_process_kill(3, msgspec.msgpack.encode({"pid": 9999}))


@pytest.mark.asyncio
async def test_runtime_mcu_special_logic_v4(service_setup: Any) -> None:
    service, state, serial, mqtt = service_setup
    state.mark_synchronized()

    # 1. Console queue flush
    state.console_to_mcu_queue.append(b"pending")
    await service._flush_console_queue()
    
    # 2. Storage lock contention
    async with service._storage_lock:
        task = asyncio.create_task(service._handle_mcu_file_write(1, msgspec.msgpack.encode(FileWritePacket(path="t", data=b""))))
        await asyncio.sleep(0.01)
        
    # 3. SPI Config JSON error
    msg = Message(topic="br/spi/config", payload=b"invalid{json", qos=0, retain=False, mid=1, properties=None)
    await service.handle_mqtt_message(msg)


@pytest.mark.asyncio
async def test_runtime_mqtt_exhaustive_v2(service_setup: Any) -> None:
    service, state, serial, mqtt = service_setup
    state.mark_synchronized()

    # 1. Shell Actions (POLL/KILL/RUN_ASYNC)
    for act in [ShellAction.POLL, ShellAction.KILL]:
        msg = Message(topic=f"br/sh/{act.value}/123", payload=b"", qos=0, retain=False, mid=1, properties=None)
        with patch("psutil.Process", return_value=MagicMock()):
            await service.handle_mqtt_message(msg)

    # 2. SPI Actions
    for act in [SpiAction.BEGIN, SpiAction.END, SpiAction.CONFIG]:
        msg = Message(topic=f"br/spi/{act.value}", payload=b"{}", qos=0, retain=False, mid=1, properties=None)
        await service.handle_mqtt_message(msg)

    # 3. Pin Actions
    for t in [Topic.DIGITAL, Topic.ANALOG]:
        msg = Message(topic=f"br/{t}/1/read", payload=b"", qos=0, retain=False, mid=1, properties=None)
        await service.handle_mqtt_message(msg)
        msg = Message(topic=f"br/{t}/1/write", payload=b"1", qos=0, retain=False, mid=1, properties=None)
        await service.handle_mqtt_message(msg)
