"""Assertive, deterministic tests for McuBridge runtime service."""

from __future__ import annotations
import asyncio
from unittest.mock import AsyncMock, patch

import pytest
import pytest_asyncio
from aiomqtt.message import Message

from mcubridge.protocol import mcubridge_pb2 as pb
from mcubridge.config.settings import RuntimeConfig
from mcubridge.protocol.protocol import Command, Status
from mcubridge.services.runtime import BridgeService
from mcubridge.state.context import RuntimeState
from mcubridge.transport.serial import SerialTransport

@pytest_asyncio.fixture
async def service_setup(
    runtime_config: RuntimeConfig, runtime_state: RuntimeState
) -> tuple[BridgeService, RuntimeState, AsyncMock, AsyncMock]:
    serial = AsyncMock(spec=SerialTransport)
    serial.send.return_value = True
    serial.send_raw.return_value = True
    serial.acknowledge.return_value = True
    service = BridgeService(runtime_config, runtime_state, serial)
    mock_mqtt = AsyncMock()
    service.set_mqtt_client(mock_mqtt)
    return service, runtime_state, serial, mock_mqtt

@pytest.mark.asyncio
async def test_mcu_file_read_handler_asserts_state(service_setup: tuple[BridgeService, RuntimeState, AsyncMock, AsyncMock]) -> None:
    service, state, serial, mock_mqtt = service_setup
    state.mark_synchronized()
    
    payload = pb.FileRead(path="test.txt").SerializeToString()
    
    with patch("pathlib.Path.read_bytes", return_value=b"file_data"):
        with patch("pathlib.Path.is_file", return_value=True):
            await service.handle_mcu_frame(Command.CMD_FILE_READ.value, 1, payload)
    
    serial.send.assert_called_once()
    assert serial.send.call_args[0][0] == Command.CMD_FILE_READ_RESP.value
    resp = pb.FileReadResponse.FromString(serial.send.call_args[0][1])
    assert resp.content == b"file_data"
    
@pytest.mark.asyncio
async def test_mqtt_file_write_asserts_serial(service_setup: tuple[BridgeService, RuntimeState, AsyncMock, AsyncMock]) -> None:
    service, state, serial, mock_mqtt = service_setup
    state.mark_synchronized()
    
    msg = Message(
        topic="br/file/write/mcu/out.txt",
        payload=b"new_data",
        qos=0,
        retain=False,
        mid=1,
        properties=None,
    )
    
    await service.handle_mqtt_message(msg)
    
    serial.send.assert_called_once()
    assert serial.send.call_args[0][0] == Command.CMD_FILE_WRITE.value
    req = pb.FileWrite.FromString(serial.send.call_args[0][1])
    assert req.path == "out.txt"
    assert req.data == b"new_data"
