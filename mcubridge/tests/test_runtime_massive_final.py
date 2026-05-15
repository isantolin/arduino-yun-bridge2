import asyncio
from unittest.mock import patch
from typing import Any, cast
from typing import Tuple, Generator
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
    FileReadResponsePacket,
    SpiTransferResponsePacket,
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
) -> Generator[Tuple[BridgeService, RuntimeState, AsyncMock], None, None]:
    config = RuntimeConfig(
        mqtt_topic="br",
        serial_port="/dev/test",
        file_system_root=str(tmp_path),
        allowed_commands=["ls"],
    )
    state = create_runtime_state(config)
    serial = AsyncMock()
    AsyncMock()
    service = BridgeService(config, state, serial)
    yield service, state, serial

    # [SIL-2] Ensure all processes and tasks are terminated to prevent ResourceWarnings
    state.cleanup()

    # Attempt to cancel tasks if loop is still running
    try:
        loop = asyncio.get_running_loop()
        if loop.is_running():
            for task in asyncio.all_tasks(loop):
                if "_monitor_process" in str(task):
                    task.cancel()
    except RuntimeError:
        pass


@pytest.mark.asyncio
async def test_runtime_brute_force_handlers_v2(
    service_setup: Tuple[BridgeService, RuntimeState, AsyncMock],
) -> None:
    service, state, _ = service_setup
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
        # Reset mocks to track per-handler calls
        cast(Any, service.serial.send).reset_mock()
        service.enqueue_mqtt = AsyncMock()

        # Test valid payload
        with patch("asyncio.create_subprocess_exec") as mock_exec:
            mock_exec.return_value = AsyncMock()
            await handler(seq, payload)

        # Determine expected behavior based on handler
        h_name = handler.__name__
        if h_name == "_handle_mcu_xoff":
            assert service.state.mcu_is_paused is True
        elif h_name == "_handle_mcu_xon":
            assert service.state.mcu_is_paused is False
        elif h_name in (
            "_handle_mcu_console_write",
            "_handle_mcu_mailbox_push",
            "_handle_mcu_datastore_put",
            "_handle_mcu_mailbox_processed",
            "_handle_mcu_spi_resp",
        ):
            assert (
                service.enqueue_mqtt.called
            ), f"Handler {h_name} should have called mqtt.enqueue_mqtt"
        elif h_name in (
            "_handle_mcu_datastore_get",
            "_handle_mcu_mailbox_read",
            "_handle_mcu_mailbox_available",
            "_handle_mcu_file_write",
            "_handle_mcu_file_read",
            "_handle_mcu_file_remove",
            "_handle_mcu_process_run",
            "_handle_mcu_process_poll",
        ):
            assert cast(
                Any, service.serial.send
            ).called, f"Handler {h_name} should have called serial.send"

        # Test invalid payload (should not crash)
        cast(Any, service.serial.send).reset_mock()
        service.enqueue_mqtt = AsyncMock()
        try:
            await handler(seq, b"\xff")
        except (asyncio.CancelledError, OSError, ValueError):
            pass


@pytest.mark.asyncio
async def test_runtime_mqtt_brute_force_v2(
    service_setup: Tuple[BridgeService, RuntimeState, AsyncMock],
) -> None:
    service, state, _ = service_setup
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
            # type: ignore
            await handler(*args)  # type: ignore
        except (asyncio.CancelledError, OSError, ValueError):
            pass
