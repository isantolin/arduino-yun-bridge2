from mcubridge.protocol import mcubridge_pb2 as pb
import asyncio
from typing import Any
from unittest.mock import AsyncMock, patch
from pathlib import Path

import pytest
from aiomqtt.message import Message

from mcubridge.services.runtime import BridgeService
from mcubridge.state.context import create_runtime_state, RuntimeState
from mcubridge.config.settings import RuntimeConfig
from mcubridge.protocol.protocol import (
    Command,
    Topic,
)
from mcubridge.protocol.structures import (
    PendingPinRequest,
)


@pytest.fixture
def service_setup(
    tmp_path: Path,
) -> tuple[BridgeService, RuntimeState, AsyncMock, AsyncMock]:
    config = RuntimeConfig(
        mqtt_topic="br",
        serial_port="/dev/test",
        file_system_root=str(tmp_path),
        allowed_commands=["ls", "test_cmd"],
    )
    state = create_runtime_state(config)
    serial = AsyncMock()
    serial.send = AsyncMock(return_value=True)
    serial.acknowledge = AsyncMock(return_value=True)
    serial.send = AsyncMock(return_value=None)
    serial.reset = AsyncMock()

    mqtt = AsyncMock()
    mqtt.enqueue_mqtt = AsyncMock()

    service = BridgeService(config, state, serial)
    service.register_serial_sender(serial.send)
    return service, state, serial, mqtt


@pytest.mark.asyncio
async def test_runtime_mcu_lifecycle_exhaustive(service_setup: Any) -> None:
    service, state, serial, _ = service_setup

    async def mock_sync() -> None:
        state.mark_synchronized()

    with patch.object(service.handshake, "synchronize", side_effect=mock_sync):
        serial.send.return_value = pb.VersionResponse(major=1, minor=0, patch=0).SerializeToString()
        await getattr(service, "_request_mcu_version")()
        assert serial.send.called

    await service.on_serial_disconnected()
    assert state.is_synchronized is False
    assert serial.reset.called


@pytest.mark.asyncio
async def test_mcu_handlers_exhaustive(service_setup: Any) -> None:
    service, state, _, _ = service_setup
    state.mark_synchronized()

    for _, handler in service.mcu_registry.items():
        try:
            await handler(1, b"\x80")
        except (asyncio.CancelledError, OSError, ValueError):
            pass
        try:
            await handler(2, b"\xff\xff")
        except (asyncio.CancelledError, OSError, ValueError):
            pass

    state.mailbox_queue.append(b"msg")
    await service.mcu_registry[Command.CMD_MAILBOX_READ.value](4, b"")

    state.pending_digital_reads.append(PendingPinRequest(pin=1, reply_context=None))
    await service.mcu_registry[Command.CMD_DIGITAL_READ_RESP.value](
        5, pb.DigitalReadResponse(value=1).SerializeToString()
    )


@pytest.mark.asyncio
async def test_mqtt_handlers_exhaustive(service_setup: Any) -> None:
    service, state, _, _ = service_setup
    state.mark_synchronized()

    topics = [
        Topic.CONSOLE,
        Topic.DIGITAL,
        Topic.ANALOG,
        Topic.SHELL,
        Topic.FILE,
        Topic.DATASTORE,
        Topic.MAILBOX,
        Topic.SPI,
        Topic.SYSTEM,
    ]

    for t in topics:
        msg = Message(
            topic=f"br/{t}/act",
            payload=b"",
            qos=0,
            retain=False,
            mid=1,
            properties=None,
        )
        try:
            await service.handle_mqtt_message(msg)
        except (asyncio.CancelledError, OSError, ValueError):
            pass

    msg = Message(
        topic="br/spi/config",
        payload=pb.SpiConfig(frequency=1000, bit_order=0, data_mode=0).SerializeToString(),
        qos=0,
        retain=False,
        mid=1,
        properties=None,
    )
    await service.handle_mqtt_message(msg)

    msg = Message(
        topic="br/sh/run_async",
        payload=b"ls",
        qos=0,
        retain=False,
        mid=1,
        properties=None,
    )
    with patch(
        "asyncio.create_subprocess_exec",
        AsyncMock(return_value=AsyncMock(pid=123, wait=AsyncMock(return_value=0))),
    ):
        await service.handle_mqtt_message(msg)

    f = Path(state.file_system_root) / "test.txt"
    f.write_bytes(b"data")
    msg = Message(
        topic="br/file/read/test.txt",
        payload=b"",
        qos=0,
        retain=False,
        mid=1,
        properties=None,
    )
    await service.handle_mqtt_message(msg)
