import pytest
import asyncio
from unittest.mock import AsyncMock, MagicMock
from mcubridge_client import Bridge
from mcubridge.protocol import mcubridge_pb2 as pb


@pytest.fixture
def mock_grpc(monkeypatch):
    mock_channel = MagicMock()
    mock_stub = MagicMock()
    
    mock_stub.Publish = AsyncMock()
    mock_stub.SubscribeConsole = MagicMock()
    
    async def empty_gen(*args, **kwargs):
        if False:
            yield None
    mock_stub.SubscribeConsole.return_value = empty_gen()

    monkeypatch.setattr("mcubridge_client.Channel", MagicMock(return_value=mock_channel))
    monkeypatch.setattr("mcubridge_client.LocalBridgeStub", MagicMock(return_value=mock_stub))
    
    return mock_channel, mock_stub


@pytest.mark.asyncio
async def test_client_connect_disconnect(mock_grpc) -> None:
    mock_channel, mock_stub = mock_grpc
    bridge = Bridge(socket_path="/var/run/test.sock")
    await bridge.connect()
    assert bridge.channel is not None
    assert bridge.stub is not None
    await bridge.disconnect()
    assert bridge.channel is None


@pytest.mark.asyncio
async def test_client_digital_write(mock_grpc) -> None:
    mock_channel, mock_stub = mock_grpc
    bridge = Bridge(socket_path="/var/run/test.sock")
    await bridge.connect()

    await bridge.digital_write(13, 1)

    assert mock_stub.Publish.called
    msg = mock_stub.Publish.call_args[0][0]
    assert msg.topic_name == "br/d/13"
    assert msg.payload == b"1"


@pytest.mark.asyncio
async def test_client_analog_write(mock_grpc) -> None:
    mock_channel, mock_stub = mock_grpc
    bridge = Bridge(socket_path="/var/run/test.sock")
    await bridge.connect()

    await bridge.analog_write(3, 128)

    assert mock_stub.Publish.called
    msg = mock_stub.Publish.call_args[0][0]
    assert msg.topic_name == "br/a/3"
    assert msg.payload == b"128"


@pytest.mark.asyncio
async def test_client_datastore_put(mock_grpc) -> None:
    mock_channel, mock_stub = mock_grpc
    bridge = Bridge(socket_path="/var/run/test.sock")
    await bridge.connect()

    resp = pb.CloudQueuedPublish(
        topic_name="br/datastore/get/test_key",
        payload=b"OK",
    )
    mock_stub.Publish.return_value = resp

    await bridge.put("test_key", "test_value")
    assert mock_stub.Publish.called
    msg = mock_stub.Publish.call_args[0][0]
    assert msg.topic_name == "br/datastore/put/test_key"
    assert msg.payload == b"test_value"


@pytest.mark.asyncio
async def test_client_file_write(mock_grpc) -> None:
    mock_channel, mock_stub = mock_grpc
    bridge = Bridge(socket_path="/var/run/test.sock")
    await bridge.connect()

    resp = pb.CloudQueuedPublish(
        topic_name="br/file/read/test.txt",
        payload=b"content",
    )
    mock_stub.Publish.return_value = resp

    await bridge.file_write("test.txt", "content")

    assert mock_stub.Publish.called
    msg = mock_stub.Publish.call_args[0][0]
    assert msg.topic_name == "br/file/write/test.txt"
    assert msg.payload == b"content"


@pytest.mark.asyncio
async def test_client_analog_read_timeout(mock_grpc) -> None:
    mock_channel, mock_stub = mock_grpc
    bridge = Bridge(socket_path="/var/run/test.sock")
    await bridge.connect()

    async def raise_timeout(*args, **kwargs):
        await asyncio.sleep(0.5)
        raise TimeoutError()
    mock_stub.Publish.side_effect = raise_timeout

    with pytest.raises(TimeoutError):
        await bridge.analog_read(0, timeout=0.05)
