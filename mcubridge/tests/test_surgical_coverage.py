import asyncio
from typing import Any
import pytest
import msgspec
from unittest.mock import AsyncMock, MagicMock, patch
from aiomqtt.message import Message
from pathlib import Path
import sys

from mcubridge.services.runtime import BridgeService
from mcubridge.transport.serial import SerialTransport
from mcubridge.state.context import create_runtime_state, RuntimeState
from mcubridge.config.settings import RuntimeConfig
from mcubridge.protocol.protocol import (
    Command,
)
from mcubridge.protocol.structures import (
    DatastoreGetResponsePacket,
    DigitalReadResponsePacket,
    AnalogReadResponsePacket,
    SpiTransferResponsePacket,
    PendingPinRequest,
)

# Mock 'uci' globally for tests that import scripts directly
sys.modules["uci"] = MagicMock()


@pytest.fixture
def service_setup(
    tmp_path: Path,
) -> tuple[BridgeService, RuntimeState, AsyncMock, AsyncMock]:
    config = RuntimeConfig(mqtt_topic="br", serial_port="/dev/test", file_system_root=str(tmp_path))
    state = create_runtime_state(config)
    serial = AsyncMock(spec=SerialTransport)
    mqtt = AsyncMock()
    service = BridgeService(config, state, serial)
    return service, state, serial, mqtt


@pytest.mark.asyncio
async def test_surgical_runtime_exhaustive(
    service_setup: tuple[BridgeService, RuntimeState, AsyncMock, AsyncMock],
) -> None:
    service, state, _, _ = service_setup
    state.mark_synchronized()

    # Test all MCU frame handlers
    # Datastore GET response
    await service.handle_mcu_frame(
        Command.CMD_DATASTORE_GET_RESP.value,
        1,
        msgspec.msgpack.encode(DatastoreGetResponsePacket(value=b"v")),
    )

    # Digital Read response
    state.pending_digital_reads.append(PendingPinRequest(pin=1, reply_context=asyncio.Future[Any]()))
    await service.handle_mcu_frame(
        Command.CMD_DIGITAL_READ_RESP.value,
        1,
        msgspec.msgpack.encode(DigitalReadResponsePacket(value=1)),
    )

    # Analog Read response
    state.pending_analog_reads.append(PendingPinRequest(pin=1, reply_context=asyncio.Future[Any]()))
    await service.handle_mcu_frame(
        Command.CMD_ANALOG_READ_RESP.value,
        1,
        msgspec.msgpack.encode(AnalogReadResponsePacket(value=512)),
    )

    # SPI Transfer response
    await service.handle_mcu_frame(
        Command.CMD_SPI_TRANSFER_RESP.value,
        1,
        msgspec.msgpack.encode(SpiTransferResponsePacket(data=b"resp")),
    )

    # Test TaskGroup cancel path
    with patch("asyncio.TaskGroup.__aexit__", side_effect=asyncio.CancelledError()):
        try:
            async with service:
                pass
        except asyncio.CancelledError:
            pass


@pytest.mark.asyncio
async def test_surgical_runtime_edge_cases(
    service_setup: tuple[BridgeService, RuntimeState, AsyncMock, AsyncMock],
) -> None:
    service, _, _, _ = service_setup

    # on_serial_connected failure
    with patch.object(service.handshake, "synchronize", side_effect=Exception("fail")):
        with pytest.raises(Exception, match="fail"):
            await service.on_serial_connected()
    # handle_mqtt_message with invalid topic
    msg = Message(topic="invalid", payload=b"", qos=0, retain=False, mid=1, properties=None)
    await service.handle_mqtt_message(msg)


def test_surgical_scripts_coverage() -> None:
    from scripts import mcubridge_file_push as file_push

    mock_mqtt = MagicMock()
    with patch("sys.argv", ["file-push", "local.txt", "remote.txt"]):
        with patch("aiomqtt.Client", return_value=mock_mqtt):
            with patch("builtins.open", MagicMock()):
                with patch.object(Path, "exists", return_value=True):
                    with patch.object(Path, "read_bytes", return_value=b"data"):
                        file_push.main()
                        assert mock_mqtt.__aenter__.called

    from scripts import mcubridge_rotate_credentials as rotate

    with patch("sys.argv", ["rotate", "--force"]):
        with patch("subprocess.run") as mock_run:
            rotate.main()
            # Verify it tried to restart the service
            assert any("/etc/init.d/mcubridge" in str(call) for call in mock_run.call_args_list)
