import asyncio
import pytest
from typing import Any, cast
from unittest.mock import MagicMock, patch, AsyncMock
from mcubridge.state.context import RuntimeState, create_runtime_state
from mcubridge.state.storage import InMemoryDeque
from mcubridge.config.settings import RuntimeConfig
from mcubridge.services.runtime import BridgeService
from mcubridge.transport.serial import SerialTransport
from mcubridge.protocol.structures import create_queued_publish
from mcubridge.protocol import mcubridge_pb2 as pb
from google.protobuf.message import EncodeError as ProtobufSerializationError


def _replace_mailbox_queue(state: RuntimeState, replacement: Any) -> None:
    if hasattr(state.mailbox_queue, "close"):
        try:
            res = cast(Any, state.mailbox_queue).close()
            if asyncio.iscoroutine(res):
                try:
                    res.send(None)
                except StopIteration:
                    pass
        except (OSError, RuntimeError):
            pass
    state.mailbox_queue = replacement


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
        # Trigger QueueEmpty in enqueue_cloud finally block
        with patch.object(state.cloud_publish_queue, "get_nowait", side_effect=asyncio.QueueEmpty()):
            await service.enqueue_cloud(create_queued_publish(topic_name="test", payload=b""))

        # Mock Deque methods to throw OSError for error branch coverage
        spool = getattr(service, "_cloud_spool")
        with patch.object(spool, "append", side_effect=OSError("DB error")):
            success = await getattr(service, "_spool_cloud_message_locked")(
                create_queued_publish(topic_name="test", payload=b"")
            )
            assert not success

        with patch.object(spool, "length", side_effect=OSError("DB error")):
            await getattr(service, "_flush_cloud_spool_locked")()
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
    real_config.cloud_queue_limit = 2
    state = create_runtime_state(real_config)
    serial = AsyncMock(spec=SerialTransport)
    service = BridgeService(real_config, state, serial)
    try:
        spool = getattr(service, "_cloud_spool")
        await spool.clear()

        msg1 = create_queued_publish(topic_name="test1", payload=b"payload1")
        msg2 = create_queued_publish(topic_name="test2", payload=b"payload2")
        msg3 = create_queued_publish(topic_name="test3", payload=b"payload3")

        assert await getattr(service, "_spool_cloud_message_locked")(msg1)
        assert await getattr(service, "_spool_cloud_message_locked")(msg2)
        assert await getattr(service, "_spool_cloud_message_locked")(msg3)

        assert await spool.length() == 2
        item1 = await spool.popleft()
        item2 = await spool.popleft()
        el1 = pb.CloudQueuedPublish.FromString(item1)
        el2 = pb.CloudQueuedPublish.FromString(item2)
        assert el1.topic_name == "test2"
        assert el2.topic_name == "test3"
        assert service.state.cloud_spool_dropped_limit == 1
        assert service.state.cloud_spool_trim_events == 1
    finally:
        service.cleanup()
        state.cleanup()


@pytest.mark.asyncio
async def test_corrupt_item_handling(real_config: RuntimeConfig) -> None:
    state = create_runtime_state(real_config)
    serial = AsyncMock(spec=SerialTransport)
    service = BridgeService(real_config, state, serial)
    mock_client = MagicMock()
    mock_client.drain = AsyncMock()
    object.__setattr__(service, "_cloud_writer", mock_client)
    try:
        spool = getattr(service, "_cloud_spool")
        await spool.clear()

        await spool.append(b"invalid_bytes_not_protobuf")
        valid_msg = create_queued_publish(topic_name="valid", payload=b"valid_payload")
        await spool.append(valid_msg.SerializeToString())

        await getattr(service, "_flush_cloud_spool_locked")()

        assert await spool.length() == 0
        assert service.state.cloud_spool_corrupt_dropped == 1
        assert mock_client.write.call_count == 2
    finally:
        service.cleanup()
        state.cleanup()


@pytest.mark.asyncio
async def test_serialization_failure(real_config: RuntimeConfig) -> None:
    state = create_runtime_state(real_config)
    serial = AsyncMock(spec=SerialTransport)
    service = BridgeService(real_config, state, serial)
    try:
        msg = create_queued_publish(topic_name="test", payload=b"payload")
        with patch(
            "mcubridge.protocol.mcubridge_pb2.CloudQueuedPublish.SerializeToString",
            side_effect=ProtobufSerializationError("Serialization error"),
        ):
            success = await getattr(service, "_spool_cloud_message_locked")(msg)
            assert not success
    finally:
        service.cleanup()
        state.cleanup()


@pytest.mark.asyncio
async def test_peeking_or_popping_errors(real_config: RuntimeConfig) -> None:
    state = create_runtime_state(real_config)
    serial = AsyncMock(spec=SerialTransport)
    service = BridgeService(real_config, state, serial)
    mock_client = MagicMock()
    mock_client.drain = AsyncMock()
    object.__setattr__(service, "_cloud_writer", mock_client)
    try:
        spool = getattr(service, "_cloud_spool")
        await spool.clear()

        valid_msg = create_queued_publish(topic_name="valid", payload=b"payload")
        await spool.append(valid_msg.SerializeToString())

        # 1. IndexError on peek
        with patch.object(spool, "peek", side_effect=IndexError("Mock empty")):
            await getattr(service, "_flush_cloud_spool_locked")()
        assert await spool.length() == 1

        # 2. OSError on peek
        with patch.object(spool, "peek", side_effect=OSError("DB error")):
            await getattr(service, "_flush_cloud_spool_locked")()
        assert state.cloud_spool_degraded
        state.cloud_spool_degraded = False

        # 3. IndexError on popleft when corrupt
        await spool.clear()
        await spool.append(b"corrupt")
        with patch.object(spool, "popleft", side_effect=IndexError("Pop empty")):
            await getattr(service, "_flush_cloud_spool_locked")()
        assert state.cloud_spool_corrupt_dropped == 0

        # 4. OSError on popleft when corrupt
        await spool.clear()
        await spool.append(b"corrupt")
        with patch.object(spool, "popleft", side_effect=OSError("DB error during pop")):
            await getattr(service, "_flush_cloud_spool_locked")()
        assert state.cloud_spool_corrupt_dropped == 0

        # 5. OSError on popleft after publish
        await spool.clear()
        await spool.append(valid_msg.SerializeToString())
        popleft_mock = MagicMock(side_effect=OSError("DB error during pop"))
        with patch.object(spool, "popleft", popleft_mock):
            await getattr(service, "_flush_cloud_spool_locked")()
        assert state.cloud_spool_degraded
    finally:
        service.cleanup()
        state.cleanup()


@pytest.mark.asyncio
async def test_mailbox_queue_close_error(real_config: RuntimeConfig) -> None:
    state = create_runtime_state(real_config)

    fake_cache = MagicMock()
    fake_cache.close.side_effect = OSError("mock error")
    object.__setattr__(state, "mailbox_queue", MagicMock(cache=fake_cache))
    _replace_mailbox_queue(state, InMemoryDeque())
