from typing import Type
from unittest.mock import AsyncMock, MagicMock, patch
from pathlib import Path

import pytest
import msgspec

# pyright: reportPrivateUsage=false
from mcubridge.services.runtime import BridgeService
from mcubridge.state.context import create_runtime_state, RuntimeState
from mcubridge.config.settings import RuntimeConfig
from mcubridge.protocol.protocol import Command
from mcubridge.protocol.structures import (
    DigitalReadResponsePacket,
    AnalogReadResponsePacket,
    DatastoreGetResponsePacket,
    SpiTransferResponsePacket,
    FileReadPacket,
)


@pytest.fixture
def service_setup(tmp_path: Path) -> tuple[BridgeService, RuntimeState, AsyncMock]:
    config = RuntimeConfig(
        mqtt_topic="br", serial_port="/dev/test", file_system_root=str(tmp_path)
    )
    state = create_runtime_state(config)
    serial = AsyncMock()
    # Ensure it returns bytes or None as expected by the code
    serial.send_and_wait_payload.return_value = None
    AsyncMock()
    service = BridgeService(config, state, serial)
    # Register mock sender
    service.register_serial_sender(serial.send)
    return service, state, serial


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
    service_setup: tuple[BridgeService, RuntimeState, AsyncMock],
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
async def test_runtime_mqtt_fuzzing(
    service_setup: tuple[BridgeService, RuntimeState, AsyncMock],
) -> None:
    """Ensure MQTT topic routes don't crash on invalid segments."""
    service, _, _ = service_setup
    from mcubridge.protocol.structures import TopicRoute
    from mcubridge.protocol.protocol import Topic
    from aiomqtt.message import Message

    # Test with non-existent system actions
    route = TopicRoute(
        raw="br/sys/invalid/action",
        prefix="br",
        topic=Topic.SYSTEM,
        segments=("invalid", "action"),
    )
    msg = Message(
        topic="br/sys/invalid/action",
        payload=b"",
        qos=0,
        retain=False,
        mid=1,
        properties=None,
    )

    # Should log warning but not crash
    await service._handle_mqtt_system(route, msg)


@pytest.mark.asyncio
async def test_runtime_process_cleanup_robustness(
    service_setup: tuple[BridgeService, RuntimeState, AsyncMock],
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
    service_setup: tuple[BridgeService, RuntimeState, AsyncMock],
) -> None:
    """Test file operations handle OS permission errors."""
    service, _, serial = service_setup

    with patch("builtins.open") as mock_open:
        mock_open.side_effect = PermissionError("EACCES")

        # Test read
        await service._handle_mcu_file_read(1, b"\x81\xa4path\xa4test")
        # Should have acknowledged with ERROR
        assert serial.send.called

        # Test write
        from mcubridge.protocol.structures import FileWritePacket

        payload = msgspec.msgpack.encode(FileWritePacket(path="test.txt", data=b"data"))
        await service._handle_mcu_file_write(1, payload)
        assert serial.send.called


@pytest.mark.asyncio
async def test_runtime_mqtt_file_spi_chaos(
    service_setup: tuple[BridgeService, RuntimeState, AsyncMock],
) -> None:
    """Fuzz File and SPI MQTT handlers with invalid payloads."""
    service, _, serial = service_setup
    from mcubridge.protocol.structures import TopicRoute, SpiTransferResponsePacket
    from mcubridge.protocol.protocol import Topic
    from aiomqtt.message import Message

    # 1. File Write with non-bytes payload
    route = TopicRoute(
        raw="br/file/write/test.txt",
        prefix="br",
        topic=Topic.FILE,
        segments=("write", "test.txt"),
    )
    msg = Message(
        topic="br/file/write/test.txt",
        payload=b"invalid_not_msgpack",
        qos=0,
        retain=False,
        mid=1,
        properties=None,
    )
    await service._handle_mqtt_file(route, msg)

    # 2. SPI Transfer with malformed msgpack
    route = TopicRoute(
        raw="br/spi/transfer", prefix="br", topic=Topic.SPI, segments=("transfer",)
    )
    msg = Message(
        topic="br/spi/transfer",
        payload=b"\xde\xad",
        qos=0,
        retain=False,
        mid=2,
        properties=None,
    )

    # Mock success response for the valid path in the handler
    serial.send_and_wait_payload.return_value = msgspec.msgpack.encode(
        SpiTransferResponsePacket(data=b"ok")
    )
    await service._handle_mqtt_spi(route, msg)

    # Mock timeout/None response
    serial.send_and_wait_payload.return_value = None
    await service._handle_mqtt_spi(route, msg)
