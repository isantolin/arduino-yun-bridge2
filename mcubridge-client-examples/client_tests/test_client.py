import pytest
import aiomqtt
from unittest.mock import AsyncMock, MagicMock
from mcubridge_client import Bridge


@pytest.fixture
def mock_client(monkeypatch):
    # [SIL-2] Use MagicMock for class and AsyncMock for instance to correctly support context manager
    mock_instance = AsyncMock(spec=aiomqtt.Client)
    mock_instance.subscribe = AsyncMock()
    mock_instance.unsubscribe = AsyncMock()
    mock_instance.publish = AsyncMock()

    mock_cls = MagicMock(return_value=mock_instance)
    monkeypatch.setattr("mcubridge_client.Client", mock_cls)
    return mock_cls


@pytest.mark.asyncio
async def test_client_connect_disconnect(mock_client) -> None:
    bridge = Bridge(host="127.0.0.1", port=1883, tls_context=None)
    await bridge.connect()
    assert bridge._client is not None
    await bridge.disconnect()
    assert bridge._client is None


@pytest.mark.asyncio
async def test_client_digital_write(mock_client) -> None:
    bridge = Bridge(host="127.0.0.1", port=1883, tls_context=None)
    await bridge.connect()
    client_instance = mock_client.return_value

    await bridge.digital_write(13, 1)

    assert client_instance.publish.called
    last_call = client_instance.publish.call_args_list[-1]
    assert "br/d/13" in last_call.args[0]
    assert last_call.args[1] == "1"


@pytest.mark.asyncio
async def test_client_analog_write(mock_client) -> None:
    bridge = Bridge(host="127.0.0.1", port=1883, tls_context=None)
    await bridge.connect()
    client_instance = mock_client.return_value

    await bridge.analog_write(3, 128)

    assert client_instance.publish.called
    last_call = client_instance.publish.call_args_list[-1]
    assert "br/a/3" in last_call.args[0]
    assert last_call.args[1] == "128"


@pytest.mark.asyncio
async def test_client_datastore_put(mock_client) -> None:
    bridge = Bridge(host="127.0.0.1", port=1883, tls_context=None)
    await bridge.connect()
    client_instance = mock_client.return_value

    async def simulate_response(*args, **kwargs):
        topic = args[0]
        if "datastore/put/" in topic:
            key = topic.split("/")[-1]
            resp_topic = f"br/datastore/get/{key}"
            props = kwargs.get("properties")
            correlation = getattr(props, "CorrelationData", None)

            # [SIL-2] Use spec=aiomqtt.Message
            msg = AsyncMock(spec=aiomqtt.Message)
            msg.topic = resp_topic
            msg.payload = b"OK"
            msg.properties = AsyncMock()
            msg.properties.CorrelationData = correlation

            if correlation in bridge._correlation_routes:
                bridge._correlation_routes[correlation].put_nowait(msg)

    client_instance.publish.side_effect = simulate_response
    await bridge.put("test_key", "test_value")
    assert client_instance.publish.called


@pytest.mark.asyncio
async def test_client_file_write(mock_client) -> None:
    bridge = Bridge(host="127.0.0.1", port=1883, tls_context=None)
    await bridge.connect()
    client_instance = mock_client.return_value

    await bridge.file_write("test.txt", "content")

    assert client_instance.publish.called
    last_call = client_instance.publish.call_args_list[-1]
    assert "br/file/write/test.txt" in last_call.args[0]
    assert last_call.args[1] == "content"


@pytest.mark.asyncio
async def test_client_analog_read_timeout(mock_client) -> None:
    bridge = Bridge(host="127.0.0.1", port=1883, tls_context=None)
    await bridge.connect()

    with pytest.raises(TimeoutError):
        await bridge.analog_read(0, timeout=0.1)


@pytest.mark.asyncio
async def test_client_analog_write_direct(mock_client) -> None:
    bridge = Bridge(host="127.0.0.1", port=1883, tls_context=None)
    await bridge.connect()
    client_instance = mock_client.return_value
    await bridge.analog_write(5, 255)
    assert client_instance.publish.called
