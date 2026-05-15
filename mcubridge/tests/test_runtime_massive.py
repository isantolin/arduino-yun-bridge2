import asyncio
from typing import Any, Tuple
from unittest.mock import AsyncMock, patch
from pathlib import Path

import pytest
import msgspec
from aiomqtt.message import Message

# pyright: reportPrivateUsage=false
from mcubridge.services.runtime import BridgeService
from mcubridge.state.context import create_runtime_state, RuntimeState
from mcubridge.config.settings import RuntimeConfig
from mcubridge.protocol.protocol import (
    Topic,
    ShellAction,
    SpiAction,
)
from mcubridge.protocol.structures import (
    FileWritePacket,
    FileReadPacket,
)


@pytest.fixture
def service_setup(
    tmp_path: Path,
) -> Tuple[BridgeService, RuntimeState, AsyncMock, AsyncMock]:
    config = RuntimeConfig(
        mqtt_topic="br",
        serial_port="/dev/test",
        file_system_root=str(tmp_path),
        allowed_commands=["ls"],
    )
    state = create_runtime_state(config)
    serial = AsyncMock()
    mqtt = AsyncMock()
    service = BridgeService(config, state, serial)
    return service, state, serial, mqtt


@pytest.mark.asyncio
async def test_runtime_error_branches_v6(service_setup: Any) -> None:
    service, state, _, _ = service_setup
    state.mark_synchronized()

    for _, handler in service.mcu_registry.items():
        try:
            await handler(1, b"\xff\xff\xff")
        except (asyncio.CancelledError, OSError, ValueError):
            pass

    with patch("builtins.open", side_effect=PermissionError()):
        await service._handle_mcu_file_write(
            1, msgspec.msgpack.encode(FileWritePacket(path="t", data=b""))
        )
        await service._handle_mcu_file_read(
            2, msgspec.msgpack.encode(FileReadPacket(path="t"))
        )


@pytest.mark.asyncio
async def test_runtime_mcu_special_logic_v4(service_setup: Any) -> None:
    service, state, _, _ = service_setup
    state.mark_synchronized()

    state.console_to_mcu_queue.append(b"pending")
    await service._flush_console_queue()

    async with service._storage_lock:
        asyncio.create_task(
            service._handle_mcu_file_write(
                1, msgspec.msgpack.encode(FileWritePacket(path="t", data=b""))
            )
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


@pytest.mark.asyncio
async def test_runtime_mqtt_exhaustive_v2(service_setup: Any) -> None:
    service, state, _, _ = service_setup
    state.mark_synchronized()

    for act in [ShellAction.POLL, ShellAction.KILL]:
        msg = Message(
            topic=f"br/sh/{act.value}/123",
            payload=b"",
            qos=0,
            retain=False,
            mid=1,
            properties=None,
        )
        for act2 in [SpiAction.BEGIN, SpiAction.END, SpiAction.CONFIG]:
            msg = Message(
                topic=f"br/spi/{act2.value}",
                payload=b"{}",
                qos=0,
                retain=False,
                mid=1,
                properties=None,
            )
            await service.handle_mqtt_message(msg)

    for t in [Topic.DIGITAL, Topic.ANALOG]:
        msg = Message(
            topic=f"br/{t}/1/read",
            payload=b"",
            qos=0,
            retain=False,
            mid=1,
            properties=None,
        )
        await service.handle_mqtt_message(msg)
        msg = Message(
            topic=f"br/{t}/1/write",
            payload=b"1",
            qos=0,
            retain=False,
            mid=1,
            properties=None,
        )
        await service.handle_mqtt_message(msg)
