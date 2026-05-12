import asyncio
import pytest
import msgspec
from unittest.mock import AsyncMock, MagicMock, patch
from aiomqtt.message import Message

from mcubridge.services.runtime import BridgeService
from mcubridge.transport.serial import SerialTransport
from mcubridge.state.context import create_runtime_state
from mcubridge.config.settings import RuntimeConfig
from mcubridge.protocol.protocol import (
    Command,
    Topic,
    SystemAction,
)
from mcubridge.protocol.structures import (
    TopicRoute,
    DatastoreGetResponsePacket,
    DigitalReadResponsePacket,
    AnalogReadResponsePacket,
    SpiTransferResponsePacket,
    PendingPinRequest,
)


@pytest.fixture
def service_setup(tmp_path):
    config = RuntimeConfig(
        mqtt_topic="br", serial_port="/dev/test", file_system_root=str(tmp_path)
    )
    state = create_runtime_state(config)
    serial = AsyncMock(spec=SerialTransport)
    mqtt = AsyncMock()
    service = BridgeService(config, state, serial, mqtt)
    return service, state, serial, mqtt


@pytest.mark.asyncio
async def test_surgical_runtime_exhaustive(service_setup):
    service, state, serial, mqtt = service_setup
    state.mark_synchronized()

    # Test all MCU frame handlers
    # Datastore GET response
    await service.handle_mcu_frame(
        Command.CMD_DATASTORE_GET_RESP.value,
        1,
        msgspec.msgpack.encode(DatastoreGetResponsePacket(value=b"v")),
    )

    # Digital Read response
    state.pending_digital_reads.append(
        PendingPinRequest(pin=1, future=asyncio.Future())
    )
    await service.handle_mcu_frame(
        Command.CMD_PIN_DIGITAL_READ_RESP.value,
        1,
        msgspec.msgpack.encode(DigitalReadResponsePacket(value=1)),
    )

    # Analog Read response
    state.pending_analog_reads.append(PendingPinRequest(pin=1, future=asyncio.Future()))
    await service.handle_mcu_frame(
        Command.CMD_PIN_ANALOG_READ_RESP.value,
        1,
        msgspec.msgpack.encode(AnalogReadResponsePacket(value=512)),
    )

    # SPI Transfer response
    await service.handle_mcu_frame(
        Command.CMD_SPI_TRANSFER_RESP.value,
        1,
        msgspec.msgpack.encode(SpiTransferResponsePacket(data=b"resp")),
    )

    # Test MQTT Command Handlers
    # ROTATE CREDENTIALS
    route = TopicRoute(
        raw="br/sys/bridge/rotate_credentials",
        prefix="br",
        topic=Topic.SYSTEM,
        segments=(SystemAction.ROTATE_CREDENTIALS,),
    )
    msg = Message(
        topic="br/sys/bridge/rotate_credentials",
        payload=b"",
        qos=0,
        retain=False,
        mid=1,
        properties=None,
    )
    with patch("subprocess.run"):
        await service._handle_mqtt_system(route, msg)

    # Test TaskGroup cancel path
    with patch("asyncio.TaskGroup.__aexit__", side_effect=asyncio.CancelledError()):
        try:
            async with service:
                pass
        except asyncio.CancelledError:
            pass


@pytest.mark.asyncio
async def test_surgical_runtime_edge_cases(service_setup):
    service, state, serial, mqtt = service_setup

    # on_serial_connected failure
    with patch.object(service.handshake, "synchronize", side_effect=Exception("fail")):
        await service.on_serial_connected()

    # handle_mqtt_message with invalid topic
    msg = Message(
        topic="invalid", payload=b"", qos=0, retain=False, mid=1, properties=None
    )
    await service.handle_mqtt_message(msg)


@pytest.mark.asyncio
async def test_surgical_scripts_coverage():
    from mcubridge.scripts import mcubridge_file_push as file_push

    with patch(
        "sys.argv", ["file-push", "--port", "/dev/test", "local.txt", "remote.txt"]
    ):
        with patch("mcubridge_client.Bridge"):
            with patch("builtins.open", MagicMock()):
                file_push.main()

    from mcubridge.scripts import mcubridge_rotate_credentials as rotate

    with patch("sys.argv", ["rotate"]):
        with patch("subprocess.run"):
            rotate.main()
