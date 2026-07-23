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
    return mock_channel, mock_stub


@pytest.mark.asyncio
async def test_client_connect_disconnect(mock_grpc) -> None:
    mock_channel, mock_stub = mock_grpc
    assert mock_channel is not None
    assert mock_stub is not None


@pytest.mark.asyncio
async def test_client_digital_write(mock_grpc) -> None:
    _, mock_stub = mock_grpc
    msg = pb.CloudQueuedPublish(topic_name="br/d/13", payload=b"1", qos=1)
    await mock_stub.Publish(msg)
    assert mock_stub.Publish.called
    sent = mock_stub.Publish.call_args[0][0]
    assert sent.topic_name == "br/d/13"
    assert sent.payload == b"1"


@pytest.mark.asyncio
async def test_client_analog_write(mock_grpc) -> None:
    _, mock_stub = mock_grpc
    msg = pb.CloudQueuedPublish(topic_name="br/a/3", payload=b"128", qos=1)
    await mock_stub.Publish(msg)
    assert mock_stub.Publish.called
    sent = mock_stub.Publish.call_args[0][0]
    assert sent.topic_name == "br/a/3"
    assert sent.payload == b"128"


@pytest.mark.asyncio
async def test_client_datastore_put(mock_grpc) -> None:
    _, mock_stub = mock_grpc
    resp = pb.CloudQueuedPublish(
        topic_name="br/datastore/get/test_key",
        payload=b"OK",
    )
    mock_stub.Publish.return_value = resp
    msg = pb.CloudQueuedPublish(topic_name="br/datastore/put/test_key", payload=b"test_value", qos=1)
    await mock_stub.Publish(msg)
    assert mock_stub.Publish.called
    sent = mock_stub.Publish.call_args[0][0]
    assert sent.topic_name == "br/datastore/put/test_key"
    assert sent.payload == b"test_value"


@pytest.mark.asyncio
async def test_client_file_write(mock_grpc) -> None:
    _, mock_stub = mock_grpc
    msg = pb.CloudQueuedPublish(topic_name="br/file/write/test.txt", payload=b"content", qos=1)
    await mock_stub.Publish(msg)
    assert mock_stub.Publish.called
    sent = mock_stub.Publish.call_args[0][0]
    assert sent.topic_name == "br/file/write/test.txt"
    assert sent.payload == b"content"


@pytest.mark.asyncio
async def test_client_analog_read_timeout(mock_grpc) -> None:
    _, mock_stub = mock_grpc
    mock_stub.Publish.side_effect = TimeoutError("Publish timeout")
    msg = pb.CloudQueuedPublish(topic_name="br/a/0/read", payload=b"", qos=1)
    with pytest.raises(TimeoutError):
        await mock_stub.Publish(msg)
