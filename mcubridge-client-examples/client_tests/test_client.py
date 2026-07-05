import pytest
import asyncio
from unittest.mock import AsyncMock
from mcubridge_client import Bridge
from mcubridge.protocol import mcubridge_pb2 as pb


@pytest.fixture
def mock_socket(monkeypatch):
    mock_reader = AsyncMock(spec=asyncio.StreamReader)
    mock_writer = AsyncMock(spec=asyncio.StreamWriter)

    mock_open = AsyncMock(return_value=(mock_reader, mock_writer))
    monkeypatch.setattr("asyncio.open_unix_connection", mock_open)
    return mock_open, mock_reader, mock_writer


@pytest.mark.asyncio
async def test_client_connect_disconnect(mock_socket) -> None:
    mock_open, mock_reader, mock_writer = mock_socket
    bridge = Bridge(socket_path="/var/run/test.sock")
    await bridge.connect()
    assert bridge.writer is not None
    assert mock_open.called
    assert mock_open.call_args[0][0] == "/var/run/test.sock"

    await bridge.disconnect()
    assert bridge.writer is None


@pytest.mark.asyncio
async def test_client_digital_write(mock_socket) -> None:
    mock_open, mock_reader, mock_writer = mock_socket
    bridge = Bridge(socket_path="/var/run/test.sock")
    await bridge.connect()

    await bridge.digital_write(13, 1)

    assert mock_writer.write.called
    calls = mock_writer.write.call_args_list
    assert len(calls) == 2

    length_bytes = calls[0][0][0]
    data_bytes = calls[1][0][0]

    assert len(length_bytes) == 4
    assert int.from_bytes(length_bytes, byteorder="big") == len(data_bytes)

    msg = pb.MqttQueuedPublish.FromString(data_bytes)
    assert msg.topic_name == "br/d/13"
    assert msg.payload == b"1"


@pytest.mark.asyncio
async def test_client_analog_write(mock_socket) -> None:
    mock_open, mock_reader, mock_writer = mock_socket
    bridge = Bridge(socket_path="/var/run/test.sock")
    await bridge.connect()

    await bridge.analog_write(3, 128)

    assert mock_writer.write.called
    calls = mock_writer.write.call_args_list
    assert len(calls) == 2

    length_bytes = calls[0][0][0]
    data_bytes = calls[1][0][0]

    assert len(length_bytes) == 4
    assert int.from_bytes(length_bytes, byteorder="big") == len(data_bytes)

    msg = pb.MqttQueuedPublish.FromString(data_bytes)
    assert msg.topic_name == "br/a/3"
    assert msg.payload == b"128"


@pytest.mark.asyncio
async def test_client_datastore_put(mock_socket) -> None:
    mock_open, mock_reader, mock_writer = mock_socket
    bridge = Bridge(socket_path="/var/run/test.sock")
    await bridge.connect()

    resp = pb.MqttQueuedPublish(
        topic_name="br/datastore/get/test_key",
        payload=b"OK",
    )

    def capture_write(data):
        if len(data) > 4:
            msg = pb.MqttQueuedPublish.FromString(data)
            resp.correlation_data = msg.correlation_data
            resp_bytes = resp.SerializeToString()
            resp_len = len(resp_bytes).to_bytes(4, byteorder="big")
            mock_reader.readexactly.side_effect = [resp_len, resp_bytes, asyncio.CancelledError()]

    mock_writer.write.side_effect = capture_write

    await bridge.put("test_key", "test_value")
    assert mock_writer.write.called


@pytest.mark.asyncio
async def test_client_file_write(mock_socket) -> None:
    mock_open, mock_reader, mock_writer = mock_socket
    bridge = Bridge(socket_path="/var/run/test.sock")
    await bridge.connect()

    await bridge.file_write("test.txt", "content")

    assert mock_writer.write.called
    calls = mock_writer.write.call_args_list
    assert len(calls) == 2

    data_bytes = calls[1][0][0]
    msg = pb.MqttQueuedPublish.FromString(data_bytes)
    assert msg.topic_name == "br/file/write/test.txt"
    assert msg.payload == b"content"


@pytest.mark.asyncio
async def test_client_analog_read_timeout(mock_socket) -> None:
    mock_open, mock_reader, mock_writer = mock_socket
    bridge = Bridge(socket_path="/var/run/test.sock")
    await bridge.connect()

    # Empty mock side effect so it hangs and times out
    mock_reader.readexactly.side_effect = asyncio.Future

    with pytest.raises(TimeoutError):
        await bridge.analog_read(0, timeout=0.1)
