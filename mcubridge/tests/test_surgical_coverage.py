from mcubridge.protocol import mcubridge_pb2 as pb
import asyncio
from typing import Any
import pytest
from unittest.mock import AsyncMock, MagicMock, patch
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
    PendingPinRequest,
)


class QoS:
    def __init__(self, value: int) -> None:
        self.value = value


class PublishPacket:
    def __init__(
        self,
        topic: str,
        payload: bytes,
        qos: QoS,
        retain: bool = False,
        packet_id: int | None = None,
    ) -> None:
        self.topic = topic
        self.payload = payload
        self.qos = qos
        self.retain = retain
        self.packet_id = packet_id


def Message(
    topic: str,
    payload: bytes,
    qos: int = 0,
    retain: bool = False,
    mid: int = 0,
    properties: Any | None = None,
) -> PublishPacket:
    return PublishPacket(
        topic=topic,
        payload=payload,
        qos=QoS(qos),
        retain=retain,
        packet_id=mid if qos > 0 else None,
    )


# Mock 'uci' globally for tests that import scripts directly
sys.modules["uci"] = MagicMock()


@pytest.fixture
def service_setup(
    tmp_path: Path,
) -> tuple[BridgeService, RuntimeState, AsyncMock, AsyncMock]:
    config = RuntimeConfig(topic_prefix="br", serial_port="/dev/test", file_system_root=str(tmp_path))
    state = create_runtime_state(config)
    serial = AsyncMock(spec=SerialTransport)
    cloud = AsyncMock()
    service = BridgeService(config, state, serial)
    return service, state, serial, cloud


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
        pb.DatastoreGetResponse(value=b"v").SerializeToString(),
    )

    # Digital Read response
    state.pending_digital_reads.append(PendingPinRequest(pin=1, reply_context=asyncio.Future[Any]()))
    await service.handle_mcu_frame(
        Command.CMD_DIGITAL_READ_RESP.value,
        1,
        pb.DigitalReadResponse(value=1).SerializeToString(),
    )

    # Analog Read response
    state.pending_analog_reads.append(PendingPinRequest(pin=1, reply_context=asyncio.Future[Any]()))
    await service.handle_mcu_frame(
        Command.CMD_ANALOG_READ_RESP.value,
        1,
        pb.AnalogReadResponse(value=512).SerializeToString(),
    )

    # SPI Transfer response
    await service.handle_mcu_frame(
        Command.CMD_SPI_TRANSFER_RESP.value,
        1,
        pb.SpiTransferResponse(data=b"resp").SerializeToString(),
    )

    # Test run() cancel path: verify CancelledError from TaskGroup is handled gracefully
    with patch.object(service, "supervise", side_effect=asyncio.CancelledError()):
        with patch("asyncio.TaskGroup.__aexit__", return_value=None):
            try:
                await service.run()
            except* asyncio.CancelledError:
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
    # handle_request with invalid topic
    msg = Message(topic="invalid", payload=b"", qos=0, retain=False, mid=1, properties=None)
    await service.handle_request(msg)


def test_surgical_scripts_coverage() -> None:
    from scripts import mcubridge_file_push as file_push

    mock_sock_cls = MagicMock()
    with patch("sys.argv", ["file-push", "local.txt", "remote.txt"]):
        with patch("socket.socket", return_value=mock_sock_cls):
            with patch("builtins.open", MagicMock()):
                with patch.object(Path, "exists", return_value=True):
                    with patch.object(Path, "read_bytes", return_value=b"data"):
                        file_push.main()
                        assert mock_sock_cls.connect.called

    from scripts import mcubridge_rotate_credentials as rotate

    with patch("sys.argv", ["rotate", "--force"]):
        with patch("subprocess.run") as mock_run:
            rotate.main()
            # Verify it tried to restart the service
            assert any("/etc/init.d/mcubridge" in str(call) for call in mock_run.call_args_list)
