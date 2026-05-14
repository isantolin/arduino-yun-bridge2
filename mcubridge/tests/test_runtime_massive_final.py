import asyncio
from typing import Any, Tuple
from unittest.mock import AsyncMock
from pathlib import Path

import pytest
import msgspec
from aiomqtt.message import Message

# pyright: reportPrivateUsage=false
from mcubridge.services.runtime import BridgeService
from mcubridge.state.context import create_runtime_state, RuntimeState
from mcubridge.config.settings import RuntimeConfig
from mcubridge.protocol.protocol import Topic
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
    PinReadPacket,
    DigitalReadResponsePacket,
    AnalogReadResponsePacket,
    MailboxProcessedPacket,
    ProcessRunAsyncPacket,
    ProcessKillPacket,
    ProcessPollPacket,
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
    service = BridgeService(config, state, serial, mqtt)
    return service, state, serial, mqtt


@pytest.mark.asyncio
async def test_runtime_brute_force_handlers_v2(service_setup: Any) -> None:
    service, state, _, _ = service_setup
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
        (service._handle_mcu_file_read_resp, 1, b"\x81\xa4data\xa4abc"),
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
        (service._handle_mcu_spi_resp, 1, b"\x81\xa4data\xa4r"),
    ]

    for handler, seq, payload in handlers:
        try:
            await handler(seq, payload)
        except (asyncio.CancelledError, OSError, ValueError):
            pass
        try:
            await handler(seq, b"\xff")
        except (asyncio.CancelledError, OSError, ValueError):
            pass


@pytest.mark.asyncio
async def test_runtime_mqtt_brute_force_v2(service_setup: Any) -> None:
    service, state, _, _ = service_setup
    state.mark_synchronized()

    msg = Message(
        topic="br/test", payload=b"{}", qos=0, retain=False, mid=1, properties=None
    )
    route = TopicRoute(
        raw="br/test", prefix="br", topic=Topic.SYSTEM, segments=("get", "version")
    )

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
        try:
            await handler(*args)
        except (asyncio.CancelledError, OSError, ValueError):
            pass
