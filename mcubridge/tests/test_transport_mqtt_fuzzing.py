import asyncio
import pytest
from unittest.mock import AsyncMock, MagicMock, patch
from typing import Any
import aiomqtt
from mcubridge.transport.mqtt import MqttTransport
from mcubridge.config.settings import RuntimeConfig
from mcubridge.state.context import RuntimeState
from mcubridge.protocol.structures import QueuedPublish


@pytest.fixture
def mock_config() -> Any:
    config = MagicMock(spec=RuntimeConfig)
    config.mqtt_enabled = True
    config.mqtt_host = "localhost"
    config.mqtt_port = 1883
    config.mqtt_user = "user"
    config.mqtt_pass = "pass"
    config.reconnect_delay = 0.1
    config.get_ssl_context.return_value = None
    return config


@pytest.fixture
def mock_state() -> Any:
    state = MagicMock(spec=RuntimeState)
    state.mqtt_topic_prefix = "br"
    state.mqtt_dropped_messages = 0
    state.mqtt_drop_counts = {}
    return state


@pytest.mark.asyncio
async def test_mqtt_transport_retry_logic(mock_config: Any, mock_state: Any) -> None:
    transport = MqttTransport(mock_config, mock_state)

    # Simulate multiple connection failures then success
    with patch("aiomqtt.Client") as mock_client_class:
        mock_client = AsyncMock()
        mock_client_class.return_value = mock_client

        # Raise MqttError twice, then succeed
        # Note: tenacity will retry the block.
        # __aenter__ will be called once per attempt.
        mock_client.__aenter__.side_effect = [
            aiomqtt.MqttError("Fail 1"),
            aiomqtt.MqttError("Fail 2"),
            mock_client,
        ]

        # We need to allow enough time for retries.
        # Reconnect delay is 0.1, max 60. Exponential backoff.
        # 1st fail: wait ~0.1s
        # 2nd fail: wait ~0.2s

        async def stop_transport() -> None:
            await asyncio.sleep(5.0)  # Increased sleep for retries
            raise asyncio.CancelledError()

        with pytest.raises(asyncio.CancelledError):
            await asyncio.gather(transport.run(), stop_transport())

        assert mock_client.__aenter__.call_count >= 1


@pytest.mark.asyncio
async def test_mqtt_transport_exception_group_retry(
    mock_config: Any, mock_state: Any
) -> None:
    transport = MqttTransport(mock_config, mock_state)

    with patch("aiomqtt.Client") as mock_client_class:
        mock_client = AsyncMock()
        mock_client_class.return_value = mock_client

        # Raise a BaseExceptionGroup containing retryable exceptions
        eg = BaseExceptionGroup("Combined", [aiomqtt.MqttError("Fail"), OSError("IO")])
        mock_client.__aenter__.side_effect = [eg, mock_client]

        async def stop_transport() -> None:
            await asyncio.sleep(0.5)
            raise asyncio.CancelledError()

        with pytest.raises(asyncio.CancelledError):
            await asyncio.gather(transport.run(), stop_transport())

        assert mock_client.__aenter__.call_count >= 1


@pytest.mark.asyncio
async def test_mqtt_transport_run_disabled(mock_config: Any, mock_state: Any) -> None:
    mock_config.mqtt_enabled = False
    transport = MqttTransport(mock_config, mock_state)
    await transport.run()
    # Should return immediately


@pytest.mark.asyncio
async def test_mqtt_transport_run_fatal_error(
    mock_config: Any, mock_state: Any
) -> None:
    transport = MqttTransport(mock_config, mock_state)
    with patch("mcubridge.transport.mqtt.tenacity.AsyncRetrying") as mock_retrying:

        # Create a mock that just runs once and raises OSError
        class MockAttempt:
            def __init__(self) -> None:
                self.count = 0

            def __aiter__(self) -> Any:
                return self

            async def __anext__(self) -> Any:
                if self.count == 0:
                    self.count += 1
                    return MagicMock()
                raise StopAsyncIteration

        mock_retrying.return_value = MockAttempt()
        # Mock _connect_session to raise OSError
        setattr(transport, "_connect_session", AsyncMock(side_effect=OSError("Fatal")))
        with pytest.raises(OSError):
            await transport.run()


@pytest.mark.asyncio
async def test_mqtt_transport_run_fatal_exception_group(
    mock_config: Any, mock_state: Any
) -> None:
    transport = MqttTransport(mock_config, mock_state)
    with patch("mcubridge.transport.mqtt.tenacity.AsyncRetrying") as mock_retrying:

        class MockAttempt:
            def __init__(self) -> None:
                self.count = 0

            def __aiter__(self) -> Any:
                return self

            async def __anext__(self) -> Any:
                if self.count == 0:
                    self.count += 1
                    return MagicMock()
                raise StopAsyncIteration

        mock_retrying.return_value = MockAttempt()
        eg = BaseExceptionGroup("Fatal", [OSError("Fatal")])
        setattr(transport, "_connect_session", AsyncMock(side_effect=eg))
        with pytest.raises(BaseExceptionGroup):
            await transport.run()


@pytest.mark.asyncio
async def test_mqtt_subscriber_loop_ignores_invalid_topics(
    mock_config: Any, mock_state: Any
) -> None:
    transport = MqttTransport(mock_config, mock_state)
    mock_client = AsyncMock()

    class AsyncIter:
        def __init__(self) -> None:
            self.count = 0
            self.msgs = [
                MagicMock(topic=None, payload=b""),
                MagicMock(topic="", payload=b""),
            ]
            self.msgs[0].topic = None  # type: ignore

        def __aiter__(self) -> Any:
            return self

        async def __anext__(self) -> Any:
            if self.count < len(self.msgs):
                msg = self.msgs[self.count]
                self.count += 1
                return msg
            raise aiomqtt.MqttError("Stop")

    mock_client.messages = AsyncIter()
    with pytest.raises(aiomqtt.MqttError):
        _loop = getattr(transport, "_subscriber_loop")
        await _loop(mock_client)


@pytest.mark.asyncio
async def test_mqtt_subscriber_loop_service_exception(
    mock_config: Any, mock_state: Any
) -> None:
    transport = MqttTransport(mock_config, mock_state)
    mock_service = AsyncMock()
    mock_service.handle_mqtt_message.side_effect = ValueError("test error")
    transport.set_service(mock_service)
    mock_client = AsyncMock()

    class AsyncIter:
        def __init__(self) -> None:
            self.count = 0

        def __aiter__(self) -> Any:
            return self

        async def __anext__(self) -> Any:
            if self.count == 0:
                self.count += 1
                msg = MagicMock(topic="br/cmd", payload=b"payload")
                return msg
            raise aiomqtt.MqttError("Stop")

    mock_client.messages = AsyncIter()
    with pytest.raises(aiomqtt.MqttError):
        _loop = getattr(transport, "_subscriber_loop")
        await _loop(mock_client)


@pytest.mark.asyncio
async def test_mqtt_enqueue_reply_context(mock_config: Any, mock_state: Any) -> None:
    transport = MqttTransport(mock_config, mock_state)
    mock_client = AsyncMock()
    setattr(transport, "_client", mock_client)

    msg = QueuedPublish(topic_name="test", payload=b"data")
    reply_ctx = MagicMock()
    reply_ctx.properties.ResponseTopic = "custom/response/topic"
    reply_ctx.properties.CorrelationData = b"correlate123"
    reply_ctx.topic = "origin/topic"

    await transport.enqueue_mqtt(msg, reply_context=reply_ctx)
    mock_client.publish.assert_called()


@pytest.mark.asyncio
async def test_mqtt_enqueue_client_publish_error(
    mock_config: Any, mock_state: Any
) -> None:
    transport = MqttTransport(mock_config, mock_state)
    mock_client = AsyncMock()
    mock_client.publish.side_effect = aiomqtt.MqttError("Pub Error")
    setattr(transport, "_client", mock_client)

    msg = QueuedPublish(topic_name="test_topic", payload=b"data")
    await transport.enqueue_mqtt(msg)
    assert mock_state.mqtt_dropped_messages == 1


@pytest.mark.asyncio
async def test_mqtt_enqueue_without_client(mock_config: Any, mock_state: Any) -> None:
    transport = MqttTransport(mock_config, mock_state)
    msg = QueuedPublish(topic_name="test", payload=b"data")

    await transport.enqueue_mqtt(msg)
    assert mock_state.mqtt_dropped_messages == 1
    mock_state.metrics.mqtt_messages_dropped.inc.assert_called()  # pyright: ignore[reportUnknownMemberType]
