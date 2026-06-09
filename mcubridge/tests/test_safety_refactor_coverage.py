import asyncio
import collections
import msgspec
import pytest
from typing import Any, cast
from unittest.mock import MagicMock, patch, AsyncMock
from mcubridge.state.context import RuntimeState, create_runtime_state
from mcubridge.config.settings import RuntimeConfig
from mcubridge.services.runtime import BridgeService
from mcubridge.transport.serial import SerialTransport
from mcubridge.protocol.structures import QueuedPublish


def _replace_mailbox_queue(state: RuntimeState, replacement: Any) -> None:
    if hasattr(state.mailbox_queue, "close"):
        try:
            cast(Any, state.mailbox_queue).close()
        except (OSError, RuntimeError):
            pass
    state.mailbox_queue = cast(collections.deque[bytes], replacement)


@pytest.mark.asyncio
async def test_metrics_cleanup_coverage(real_config: RuntimeConfig) -> None:
    from mcubridge.metrics import PrometheusExporter

    state = create_runtime_state(real_config)
    try:
        exporter = PrometheusExporter(state, host="127.0.0.1", port=0)

        # Mock DBM resource with a closing failure
        mock_mq = MagicMock()
        mock_mq.close.side_effect = RuntimeError("Mock cleanup failure")

        _replace_mailbox_queue(state, mock_mq)

        # Trigger KeyError in unregister to hit the new except block
        with patch.object(getattr(exporter, "_registry"), "unregister", side_effect=KeyError()):
            with patch.object(getattr(exporter, "_server"), "serve_forever", return_value=None):
                with patch.object(getattr(exporter, "_server"), "shutdown", return_value=None):
                    await exporter.run()
    finally:
        state.cleanup()


@pytest.mark.asyncio
async def test_context_cleanup_coverage(real_config: RuntimeConfig) -> None:
    state = create_runtime_state(real_config)
    try:
        # Mock a process that fails to terminate
        mock_proc = MagicMock()
        mock_proc.handle = MagicMock()
        mock_proc.handle.terminate.side_effect = ProcessLookupError("Mock process gone")
        state.running_processes[123] = mock_proc

        # Mock dbm with AttributeError
        mock_mq = MagicMock()
        mock_mq.cache = MagicMock()
        _replace_mailbox_queue(state, mock_mq)
    finally:
        state.cleanup()  # Hits new except blocks in context.py


@pytest.mark.asyncio
async def test_runtime_safety_coverage(real_config: RuntimeConfig) -> None:

    state = create_runtime_state(real_config)
    serial = AsyncMock(spec=SerialTransport)
    service = BridgeService(real_config, state, serial)
    try:
        # Trigger QueueEmpty in enqueue_mqtt finally block
        with patch.object(state.mqtt_publish_queue, "get_nowait", side_effect=asyncio.QueueEmpty()):
            await service.enqueue_mqtt(QueuedPublish(topic_name="test", payload=b""))

        # Mock Deque methods to throw OSError for error branch coverage
        spool = getattr(service, "_mqtt_spool")
        with patch.object(spool, "append", side_effect=OSError("DB error")):
            success = await getattr(service, "_spool_mqtt_message_locked")(
                QueuedPublish(topic_name="test", payload=b"")
            )
            assert success is False

        with patch("asyncio.to_thread", side_effect=OSError("DB error")):
            await getattr(service, "_flush_mqtt_spool_locked")()
    finally:
        service.cleanup()
        state.cleanup()


@pytest.mark.asyncio
async def test_additional_coverage_boost(real_config: RuntimeConfig) -> None:
    state = create_runtime_state(real_config)
    try:
        # Trigger the logging.warning in _close_dbm_resource via Exception
        mock_mq = MagicMock()
        mock_mq.cache = MagicMock()
        mock_mq.cache.close.side_effect = RuntimeError("Fatal cleanup error")
        _replace_mailbox_queue(state, mock_mq)
    finally:
        # _close_dbm_resource catches Exception internally — cleanup() must not raise.
        state.cleanup()


@pytest.mark.asyncio
async def test_spool_trim_and_limit(real_config: RuntimeConfig) -> None:
    real_config.mqtt_queue_limit = 2
    state = create_runtime_state(real_config)
    serial = AsyncMock(spec=SerialTransport)
    service = BridgeService(real_config, state, serial)
    try:
        spool = getattr(service, "_mqtt_spool")
        spool.clear()

        msg1 = QueuedPublish(topic_name="test1", payload=b"payload1")
        msg2 = QueuedPublish(topic_name="test2", payload=b"payload2")
        msg3 = QueuedPublish(topic_name="test3", payload=b"payload3")

        assert await getattr(service, "_spool_mqtt_message_locked")(msg1) is True
        assert await getattr(service, "_spool_mqtt_message_locked")(msg2) is True
        assert await getattr(service, "_spool_mqtt_message_locked")(msg3) is True

        assert len(spool) == 2
        el1 = msgspec.msgpack.decode(spool[0], type=QueuedPublish)
        el2 = msgspec.msgpack.decode(spool[1], type=QueuedPublish)
        assert el1.topic_name == "test2"
        assert el2.topic_name == "test3"
        assert service.state.mqtt_spool_dropped_limit == 1
        assert service.state.mqtt_spool_trim_events == 1
    finally:
        service.cleanup()
        state.cleanup()


@pytest.mark.asyncio
async def test_corrupt_item_handling(real_config: RuntimeConfig) -> None:
    state = create_runtime_state(real_config)
    serial = AsyncMock(spec=SerialTransport)
    service = BridgeService(real_config, state, serial)
    mock_client = AsyncMock()
    service.set_mqtt_client(mock_client)
    try:
        spool = getattr(service, "_mqtt_spool")
        spool.clear()

        spool.append(b"invalid_bytes_not_msgpack")
        valid_msg = QueuedPublish(topic_name="valid", payload=b"valid_payload")
        spool.append(msgspec.msgpack.encode(valid_msg))

        await getattr(service, "_flush_mqtt_spool_locked")()

        assert len(spool) == 0
        assert service.state.mqtt_spool_corrupt_dropped == 1
        mock_client.publish.assert_awaited_once()
    finally:
        service.cleanup()
        state.cleanup()


@pytest.mark.asyncio
async def test_serialization_failure(real_config: RuntimeConfig) -> None:
    state = create_runtime_state(real_config)
    serial = AsyncMock(spec=SerialTransport)
    service = BridgeService(real_config, state, serial)
    try:
        msg = QueuedPublish(topic_name="test", payload=b"payload")
        with patch("msgspec.msgpack.encode", side_effect=msgspec.MsgspecError("Serialization error")):
            success = await getattr(service, "_spool_mqtt_message_locked")(msg)
            assert success is False
    finally:
        service.cleanup()
        state.cleanup()


@pytest.mark.asyncio
async def test_peeking_or_popping_errors(real_config: RuntimeConfig) -> None:

    state = create_runtime_state(real_config)
    serial = AsyncMock(spec=SerialTransport)
    service = BridgeService(real_config, state, serial)
    mock_client = AsyncMock()
    service.set_mqtt_client(mock_client)
    try:
        spool = getattr(service, "_mqtt_spool")
        spool.clear()

        valid_msg = QueuedPublish(topic_name="valid", payload=b"payload")
        spool.append(msgspec.msgpack.encode(valid_msg))

        # 1. IndexError on peek
        def mock_to_thread_index_error(func: Any, *args: Any, **kwargs: Any) -> Any:
            if func == len:
                return 1
            raise IndexError("Mock empty")

        with patch("asyncio.to_thread", side_effect=mock_to_thread_index_error):
            await getattr(service, "_flush_mqtt_spool_locked")()
        assert len(spool) == 1

        # 2. OSError on peek
        def mock_to_thread_sqlite_error(func: Any, *args: Any, **kwargs: Any) -> Any:
            if func == len:
                return 1
            raise OSError("DB error")

        with patch("asyncio.to_thread", side_effect=mock_to_thread_sqlite_error):
            await getattr(service, "_flush_mqtt_spool_locked")()
        assert state.mqtt_spool_degraded is True
        state.mqtt_spool_degraded = False

        # 3. IndexError on popleft when corrupt
        spool.clear()
        spool.append(b"corrupt")
        popleft_mock = MagicMock(side_effect=IndexError("Pop empty"))
        with patch.object(spool, "popleft", popleft_mock):
            await getattr(service, "_flush_mqtt_spool_locked")()
        assert state.mqtt_spool_corrupt_dropped == 0

        # 4. OSError on popleft when corrupt
        spool.clear()
        spool.append(b"corrupt")
        popleft_mock = MagicMock(side_effect=OSError("DB error during pop"))
        with patch.object(spool, "popleft", popleft_mock):
            await getattr(service, "_flush_mqtt_spool_locked")()
        assert state.mqtt_spool_corrupt_dropped == 0

        # 5. OSError on popleft after publish
        spool.clear()
        spool.append(msgspec.msgpack.encode(valid_msg))
        popleft_mock = MagicMock(side_effect=OSError("DB error during pop"))
        with patch.object(spool, "popleft", popleft_mock):
            await getattr(service, "_flush_mqtt_spool_locked")()
        assert state.mqtt_spool_degraded is True
    finally:
        service.cleanup()
        state.cleanup()


@pytest.mark.asyncio
async def test_mailbox_queue_close_error(real_config: RuntimeConfig) -> None:
    state = create_runtime_state(real_config)

    class FakeCache:
        def close(self):
            raise OSError("mock error")

    object.__setattr__(state, "mailbox_queue", MagicMock(cache=FakeCache()))
    _replace_mailbox_queue(state, collections.deque())
