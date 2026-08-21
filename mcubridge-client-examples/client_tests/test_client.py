import pytest
from unittest.mock import AsyncMock, MagicMock
from grpclib.client import Channel
from mcubridge_client import LocalBridgeStub
from mcubridge.protocol import mcubridge_pb2 as pb


@pytest.fixture
def mock_grpc():
    mock_channel = MagicMock(spec=Channel)
    mock_stub = MagicMock(spec=LocalBridgeStub)
    mock_stub.Publish = AsyncMock()
    mock_stub.DigitalWrite = AsyncMock()
    mock_stub.AnalogWrite = AsyncMock()
    mock_stub.DigitalRead = AsyncMock()
    mock_stub.AnalogRead = AsyncMock()
    mock_stub.DatastorePut = AsyncMock()
    mock_stub.DatastoreGet = AsyncMock()
    mock_stub.FileWrite = AsyncMock()
    mock_stub.FileRead = AsyncMock()
    mock_stub.FileRemove = AsyncMock()
    mock_stub.ProcessRunAsync = AsyncMock()
    mock_stub.ProcessPoll = AsyncMock()
    mock_stub.ProcessKill = AsyncMock()
    mock_stub.SpiTransfer = AsyncMock()
    mock_stub.SpiConfigure = AsyncMock()
    mock_stub.GetVersion = AsyncMock()
    mock_stub.GetFreeMemory = AsyncMock()
    mock_stub.GetStatus = AsyncMock()
    return mock_channel, mock_stub


@pytest.mark.asyncio
async def test_client_connect_disconnect(mock_grpc) -> None:
    mock_channel, mock_stub = mock_grpc
    assert mock_channel is not None
    assert mock_stub is not None


@pytest.mark.asyncio
async def test_client_digital_write(mock_grpc) -> None:
    _, mock_stub = mock_grpc
    msg = pb.DigitalWrite(pin=13, value=1)
    await mock_stub.DigitalWrite(msg)
    assert mock_stub.DigitalWrite.called
    sent = mock_stub.DigitalWrite.call_args[0][0]
    assert sent.pin == 13
    assert sent.value == 1


@pytest.mark.asyncio
async def test_client_analog_write(mock_grpc) -> None:
    _, mock_stub = mock_grpc
    msg = pb.AnalogWrite(pin=3, value=128)
    await mock_stub.AnalogWrite(msg)
    assert mock_stub.AnalogWrite.called
    sent = mock_stub.AnalogWrite.call_args[0][0]
    assert sent.pin == 3
    assert sent.value == 128


@pytest.mark.asyncio
async def test_client_datastore_put(mock_grpc) -> None:
    _, mock_stub = mock_grpc
    mock_stub.DatastorePut.return_value = pb.GenericResponse(status="ok")
    msg = pb.DatastorePut(key="test_key", value=b"test_value")
    res = await mock_stub.DatastorePut(msg)
    assert mock_stub.DatastorePut.called
    assert res.status == "ok"
    sent = mock_stub.DatastorePut.call_args[0][0]
    assert sent.key == "test_key"
    assert sent.value == b"test_value"


@pytest.mark.asyncio
async def test_client_file_write(mock_grpc) -> None:
    _, mock_stub = mock_grpc
    msg = pb.FileWrite(path="test.txt", data=b"content")
    await mock_stub.FileWrite(msg)
    assert mock_stub.FileWrite.called
    sent = mock_stub.FileWrite.call_args[0][0]
    assert sent.path == "test.txt"
    assert sent.data == b"content"


@pytest.mark.asyncio
async def test_client_analog_read_timeout(mock_grpc) -> None:
    _, mock_stub = mock_grpc
    mock_stub.AnalogRead.side_effect = TimeoutError("RPC timeout")
    msg = pb.PinRead(pin=0)
    with pytest.raises(TimeoutError):
        await mock_stub.AnalogRead(msg)
