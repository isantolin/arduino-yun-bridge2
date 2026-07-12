import asyncio
import pytest
from typing import Any
from unittest.mock import AsyncMock, patch
from pathlib import Path

from mcubridge.state.storage import InMemoryDeque, SqliteDeque, SqliteCache
from mcubridge.state.context import create_runtime_state
from mcubridge.config.settings import RuntimeConfig, load_runtime_config
from mcubridge.services.handshake import SerialHandshakeManager, derive_serial_timing
from mcubridge.transport.serial import SerialTransport
from mcubridge.protocol import mcubridge_pb2 as pb
from mcubridge.protocol.protocol import Command, Status
from mcubridge.protocol.structures import create_cloud_tls_context, replace_queued_publish, resolve_cloud_context
from mcubridge.protocol.topics import get_topic_for_message
from mcubridge.config.logging import configure_logging


@pytest.mark.asyncio
async def test_in_memory_deque() -> None:
    dq = InMemoryDeque(maxlen=2)
    assert len(dq) == 0
    await dq.append(b"a")
    assert len(dq) == 1
    assert await dq.length() == 1
    assert await dq.peek() == b"a"

    await dq.append(b"b")
    await dq.append(b"c")  # Triggers maxlen trim
    assert len(dq) == 2
    assert await dq.peek() == b"b"

    assert await dq.popleft() == b"b"
    assert await dq.popleft() == b"c"

    with pytest.raises(IndexError):
        await dq.peek()

    with pytest.raises(IndexError):
        await dq.popleft()

    await dq.append(b"d")
    await dq.clear()
    assert len(dq) == 0
    await dq.vacuum()
    await dq.close()


@pytest.mark.asyncio
async def test_sqlite_deque_edge_cases(tmp_path: Path) -> None:
    db_path = str(tmp_path / "test_deque.db")
    dq = SqliteDeque(path=db_path, maxlen=2)
    assert await dq.length() == 0

    # Test empty popleft and peek
    with pytest.raises(IndexError):
        await dq.popleft()

    with pytest.raises(IndexError):
        await dq.peek()

    # Test maxlen trim
    await dq.append(b"1")
    await dq.append(b"2")
    await dq.append(b"3")
    assert await dq.length() == 2
    assert await dq.peek() == b"2"

    # Test clear
    await dq.clear()
    assert await dq.length() == 0

    # Test db corruption/recreation logic
    # Write invalid data to db file to cause SQLite errors
    Path(db_path).write_bytes(b"corrupt data")
    # Trigger execute which should catch error and recreate
    await dq.append(b"new")
    assert await dq.length() == 1
    assert await dq.peek() == b"new"

    await dq.close()


@pytest.mark.asyncio
async def test_sqlite_cache_edge_cases(tmp_path: Path) -> None:
    db_path = str(tmp_path / "test_cache.db")
    cache = SqliteCache(path=db_path)

    # Test default get
    assert await cache.get("nonexistent", b"def") == b"def"

    # Test clear
    await cache.set("k", b"v")
    assert await cache.get("k", b"") == b"v"
    await cache.clear()
    assert await cache.get("k", b"") == b""

    # Test exception paths by corrupting SQLite file
    Path(db_path).write_bytes(b"corrupt data")
    assert await cache.get("k", b"fallback") == b"fallback"
    await cache.close()


@pytest.mark.asyncio
async def test_runtime_state_coverage(tmp_path: Path) -> None:
    config = RuntimeConfig(
        topic_prefix="br",
        serial_port="/dev/test",
        file_system_root=str(tmp_path),
        cloud_enabled=True,
        cloud_spool_dir=str(tmp_path / "spool"),
    )
    state = create_runtime_state(config)
    assert state.handshake_failures == 0

    # Record supervisor failures
    state.record_supervisor_failure("task1", 1.0, ValueError("err"))
    state.mark_supervisor_healthy("task1")

    # Apply observation metrics
    state.apply_handshake_stats(
        {
            "attempts": 5,
            "successes": 4,
            "failure_streak": 0,
            "last_unix": 12345.0,
        }
    )
    assert state.handshake_failures == 1

    getattr(state, "_apply_spool_observation")(
        {
            "corrupt_dropped": 2,
            "dropped_due_to_limit": 1,
            "trim_events": 3,
            "last_trim_unix": 54321.0,
        }
    )

    # Test build snaps
    state.serial_pipeline_inflight = {"event": "tx", "command_id": 1, "attempt": 1}
    state.serial_pipeline_last = {"event": "rx", "command_id": 2, "status": 0}
    snap = state.build_serial_pipeline_snapshot()
    assert snap.inflight.command_id == 1
    assert snap.last_completion.command_id == 2

    # Test configure with error path
    with patch("mcubridge.state.context.SqliteDeque", side_effect=OSError):
        state.configure()

    # Test cleanup
    state.cleanup()


@pytest.mark.asyncio
async def test_handshake_manager_edge_cases() -> None:
    config = RuntimeConfig(serial_shared_secret=b"shared", serial_handshake_fatal_failures=1)
    state = create_runtime_state(config)
    send_frame = AsyncMock(return_value=True)
    enqueue_cloud = AsyncMock()
    acknowledge_frame = AsyncMock()

    manager = SerialHandshakeManager(
        config=config,
        state=state,
        serial_timing=derive_serial_timing(config),
        send_frame=send_frame,
        enqueue_cloud=enqueue_cloud,
        acknowledge_frame=acknowledge_frame,
    )

    # Calculate session key
    key = manager.calculate_session_key(b"shared", b"\x00" * 16)
    assert len(key) == 32

    # Calculate tag
    tag = manager.calculate_handshake_tag(b"shared", b"\x00" * 16)
    assert len(tag) == 16

    # handle_link_sync_resp sync/mismatch
    state.link_handshake_nonce = b"\x00" * 16
    state.link_expected_tag = b"\x01" * 16
    resp = pb.LinkSync(nonce=b"\x00" * 16, tag=b"\x02" * 16)
    assert not await manager.handle_link_sync_resp(1, resp.SerializeToString())

    # Wait for sync timeout
    async def mock_wait() -> Any:
        await asyncio.sleep(0.01)
        raise TimeoutError()

    state.link_sync_event.wait = mock_wait
    res = await manager.synchronize()
    assert not res


@pytest.mark.asyncio
async def test_serial_transport_failures() -> None:
    config = RuntimeConfig(serial_port="/dev/nonexistent")
    state = create_runtime_state(config)
    service = AsyncMock()
    transport = SerialTransport(config, state, service)

    # reset
    await transport.reset()

    # Negotiation failure
    mock_serial = AsyncMock()
    mock_serial.send = AsyncMock(return_value=False)
    transport.serial = mock_serial
    res = await getattr(transport, "_negotiate_baudrate")(115200)
    assert not res


@pytest.mark.asyncio
async def test_runtime_additional_coverage(tmp_path: Path) -> None:
    from mcubridge.services.runtime import BridgeService

    config = RuntimeConfig(
        topic_prefix="br",
        serial_port="/dev/test",
        file_system_root=str(tmp_path),
        cloud_enabled=True,
        cloud_spool_dir=str(tmp_path / "spool"),
    )
    state = create_runtime_state(config)
    serial = AsyncMock(spec=SerialTransport)
    service = BridgeService(config, state, serial)

    # 1. on_serial_disconnected
    await service.on_serial_disconnected()

    # 2. _publish_cloud_message variants
    mock_stream = AsyncMock()
    setattr(service, "_cloud_stream", mock_stream)

    # Correlation data (Command response)
    msg_corr = pb.CloudQueuedPublish(
        topic_name="br/d/13",
        payload=b"resp",
        correlation_data=b"\x00\x00\x00\x01",
    )
    assert await getattr(service, "_publish_cloud_message")(msg_corr)

    # Telemetry metrics topic
    msg_metrics = pb.CloudQueuedPublish(
        topic_name="br/telemetry/metrics",
        payload=b"metrics_data",
    )
    assert await getattr(service, "_publish_cloud_message")(msg_metrics)

    # Telemetry summary topic
    msg_summary = pb.CloudQueuedPublish(
        topic_name="br/telemetry/summary",
        payload=b"summary_data",
    )
    assert await getattr(service, "_publish_cloud_message")(msg_summary)

    # Telemetry handshake topic
    msg_handshake = pb.CloudQueuedPublish(
        topic_name="br/telemetry/handshake",
        payload=b"handshake_data",
    )
    assert await getattr(service, "_publish_cloud_message")(msg_handshake)

    # Telemetry other topic
    msg_other = pb.CloudQueuedPublish(
        topic_name="br/telemetry/other",
        payload=b"other_data",
    )
    assert await getattr(service, "_publish_cloud_message")(msg_other)

    # 3. _flush_cloud_spool_locked with corrupt and valid entries
    spool = InMemoryDeque()
    setattr(service, "_cloud_spool", spool)

    # Corrupt entry
    await spool.append(b"corrupt protobuf payload")
    # Valid entry
    await spool.append(msg_other.SerializeToString())

    await getattr(service, "_flush_cloud_spool_locked")()

    # 4. cleanup and __del__
    service.cleanup()
    service.__del__()


@pytest.mark.asyncio
async def test_protocol_structures_edge_cases(tmp_path: Path) -> None:
    # 1. create_cloud_tls_context mTLS error paths
    cfg = pb.RuntimeConfig(
        cloud_cafile=str(tmp_path / "ca.crt"),
        cloud_certfile=str(tmp_path / "cert.crt"),
        cloud_tls_insecure=True,
    )
    # ca path doesn't exist
    with pytest.raises(RuntimeError):
        create_cloud_tls_context(cfg)

    # keyfile missing
    tmp_path.joinpath("ca.crt").touch()
    with pytest.raises(RuntimeError):
        create_cloud_tls_context(cfg)

    # 2. replace_queued_publish options
    msg = pb.CloudQueuedPublish(topic_name="a", payload=b"b")
    msg.user_properties.add(key="k", value="v")
    replaced = replace_queued_publish(
        msg,
        topic_name="c",
        user_properties=[("k2", "v2")],
        subscription_identifier=[1, 2],
    )
    assert replaced.topic_name == "c"
    assert replaced.user_properties[0].key == "k2"
    assert list(replaced.subscription_identifier) == [1, 2]

    # 3. resolve_cloud_context properties
    class MockContext:
        class MockProperties:
            ResponseTopic = "resp_topic"
            CorrelationData = b"corr"

        properties = MockProperties()
        topic = "req_topic"

    resolved = resolve_cloud_context(msg, MockContext())
    assert resolved.topic_name == "resp_topic"
    assert resolved.correlation_data == b"corr"


def test_logging_under_stream_fallback(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("MCUBRIDGE_LOG_STREAM", "1")
    cfg = pb.RuntimeConfig(debug=True)
    configure_logging(cfg)


def test_topics_get_topic_for_message_variants() -> None:
    assert get_topic_for_message("br", 1) is not None
    assert get_topic_for_message("br", "TelemetryReport") is not None
    assert get_topic_for_message("br", object()) is None


def test_settings_load_runtime_config_edge_cases() -> None:
    # Coercion coercion ValueError fallback
    from mcubridge.config.settings import _coerce_value
    from google.protobuf.descriptor import FieldDescriptor

    assert _coerce_value("not_int", FieldDescriptor.TYPE_UINT32) == 0
    assert _coerce_value("not_float", FieldDescriptor.TYPE_FLOAT) == 0.0
    assert _coerce_value(None, FieldDescriptor.TYPE_UINT32) is None
    assert _coerce_value("a", FieldDescriptor.TYPE_GROUP) == "a"

    # load_runtime_config auth dynamic fallback
    overrides = {
        "allow_digital_write": "true",
        "cloud_allow_analog_write": "yes",
    }
    cfg = load_runtime_config(overrides)
    assert cfg.topic_authorization.digital_write is True
    assert cfg.topic_authorization.analog_write is True


@pytest.mark.asyncio
async def test_mcu_frame_handlers_exhaustive_extended(tmp_path: Path) -> None:
    from mcubridge.services.runtime import BridgeService

    config = RuntimeConfig(
        topic_prefix="br",
        serial_port="/dev/test",
        file_system_root=str(tmp_path),
        cloud_enabled=True,
        cloud_spool_dir=str(tmp_path / "spool"),
    )
    state = create_runtime_state(config)
    state.mark_synchronized()
    serial = AsyncMock(spec=SerialTransport)
    service = BridgeService(config, state, serial)

    # 1. Datastore get/put with cache
    await service.handle_mcu_frame(Command.CMD_DATASTORE_PUT.value, 1, pb.DatastorePut(key="k", value=b"v"))
    await service.handle_mcu_frame(Command.CMD_DATASTORE_GET.value, 2, pb.DatastoreGet(key="k"))
    serial.send.assert_called_with(Command.CMD_DATASTORE_GET_RESP.value, pb.DatastoreGetResponse(value=b"v"))

    # 2. Mailbox push and read
    await service.handle_mcu_frame(Command.CMD_MAILBOX_PUSH.value, 3, pb.MailboxPush(data=b"hello"))
    # Non-empty read
    await service.handle_mcu_frame(Command.CMD_MAILBOX_READ.value, 4, b"")

    # 3. File operations: safe paths vs unsafe paths
    # Unsafe file write
    await service.handle_mcu_frame(Command.CMD_FILE_WRITE.value, 5, pb.FileWrite(path="../etc/passwd", data=b"data"))
    serial.send.assert_called_with(Status.ERROR.value, pb.GenericResponse(message="Write failed"))

    # Safe file read empty vs non-empty
    safe_file = tmp_path / "empty.txt"
    safe_file.touch()
    await service.handle_mcu_frame(Command.CMD_FILE_READ.value, 6, pb.FileRead(path="empty.txt"))

    safe_file_data = tmp_path / "data.txt"
    safe_file_data.write_bytes(b"some data bytes")
    await service.handle_mcu_frame(Command.CMD_FILE_READ.value, 7, pb.FileRead(path="data.txt"))

    # Unsafe file read
    await service.handle_mcu_frame(Command.CMD_FILE_READ.value, 8, pb.FileRead(path="../passwd"))
    serial.send.assert_called_with(Status.ERROR.value, pb.GenericResponse(message="Read failed"))

    # Safe file remove
    await service.handle_mcu_frame(Command.CMD_FILE_REMOVE.value, 9, pb.FileRemove(path="data.txt"))
    # Unsafe file remove
    await service.handle_mcu_frame(Command.CMD_FILE_REMOVE.value, 10, pb.FileRemove(path="../passwd"))

    # File Read Response without pending
    await service.handle_mcu_frame(Command.CMD_FILE_READ_RESP.value, 11, pb.FileReadResponse(content=b"data"))
