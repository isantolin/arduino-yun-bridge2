from __future__ import annotations

from typing import cast
from unittest.mock import AsyncMock, MagicMock
import pytest
from grpclib.client import Channel
from mcubridge_client import LocalBridgeStub
from mcubridge.protocol import mcubridge_pb2 as pb


@pytest.fixture
def mock_grpc() -> tuple[MagicMock, MagicMock]:
    mock_channel = MagicMock(spec=Channel)
    mock_stub = MagicMock(spec=LocalBridgeStub(mock_channel))
    mock_stub.DigitalWrite = AsyncMock()
    mock_stub.AnalogWrite = AsyncMock()
    mock_stub.DatastorePut = AsyncMock()
    mock_stub.FileWrite = AsyncMock()
    mock_stub.AnalogRead = AsyncMock()
    return mock_channel, mock_stub


@pytest.mark.asyncio
async def test_client_connect_disconnect(mock_grpc: tuple[MagicMock, MagicMock]) -> None:
    mock_channel, mock_stub = mock_grpc
    assert mock_channel is not None
    assert mock_stub is not None


@pytest.mark.asyncio
async def test_client_digital_write(mock_grpc: tuple[MagicMock, MagicMock]) -> None:
    _, mock_stub = mock_grpc
    msg = pb.DigitalWrite(pin=13, value=1)
    await mock_stub.DigitalWrite(msg)
    mock_fn = cast(AsyncMock, mock_stub.DigitalWrite)
    mock_fn.assert_awaited_once_with(msg)
    call_args = mock_fn.call_args
    assert call_args is not None
    sent = cast(pb.DigitalWrite, call_args[0][0])
    assert sent.pin == 13
    assert sent.value == 1


@pytest.mark.asyncio
async def test_client_analog_write(mock_grpc: tuple[MagicMock, MagicMock]) -> None:
    _, mock_stub = mock_grpc
    msg = pb.AnalogWrite(pin=3, value=128)
    await mock_stub.AnalogWrite(msg)
    mock_fn = cast(AsyncMock, mock_stub.AnalogWrite)
    mock_fn.assert_awaited_once_with(msg)
    call_args = mock_fn.call_args
    assert call_args is not None
    sent = cast(pb.AnalogWrite, call_args[0][0])
    assert sent.pin == 3
    assert sent.value == 128


@pytest.mark.asyncio
async def test_client_datastore_put(mock_grpc: tuple[MagicMock, MagicMock]) -> None:
    _, mock_stub = mock_grpc
    mock_fn = cast(AsyncMock, mock_stub.DatastorePut)
    mock_fn.return_value = pb.GenericResponse(status="ok")
    msg = pb.DatastorePut(key="test_key", value=b"test_value")
    res = cast(pb.GenericResponse, await mock_stub.DatastorePut(msg))
    mock_fn.assert_awaited_once_with(msg)
    assert res.status == "ok"
    call_args = mock_fn.call_args
    assert call_args is not None
    sent = cast(pb.DatastorePut, call_args[0][0])
    assert sent.key == "test_key"
    assert sent.value == b"test_value"


@pytest.mark.asyncio
async def test_client_file_write(mock_grpc: tuple[MagicMock, MagicMock]) -> None:
    _, mock_stub = mock_grpc
    msg = pb.FileWrite(path="test.txt", data=b"content")
    await mock_stub.FileWrite(msg)
    mock_fn = cast(AsyncMock, mock_stub.FileWrite)
    mock_fn.assert_awaited_once_with(msg)
    call_args = mock_fn.call_args
    assert call_args is not None
    sent = cast(pb.FileWrite, call_args[0][0])
    assert sent.path == "test.txt"
    assert sent.data == b"content"


@pytest.mark.asyncio
async def test_client_analog_read_timeout(mock_grpc: tuple[MagicMock, MagicMock]) -> None:
    _, mock_stub = mock_grpc
    mock_fn = cast(AsyncMock, mock_stub.AnalogRead)
    mock_fn.side_effect = TimeoutError("RPC timeout")
    msg = pb.PinRead(pin=0)
    with pytest.raises(TimeoutError):
        await mock_stub.AnalogRead(msg)
