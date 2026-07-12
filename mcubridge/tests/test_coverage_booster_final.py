import asyncio
import pytest
from unittest.mock import AsyncMock, MagicMock, patch
from pathlib import Path

from mcubridge.state.storage import InMemoryDeque, SqliteDeque, SqliteCache
from mcubridge.state.context import create_runtime_state
from mcubridge.config.settings import RuntimeConfig
from mcubridge.services.handshake import SerialHandshakeManager
from mcubridge.transport.serial import SerialTransport
from mcubridge.protocol import mcubridge_pb2 as pb


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
    config = RuntimeConfig(serial_shared_secret=b"shared")
    state = create_runtime_state(config)
    send_frame = AsyncMock(return_value=True)
    enqueue_cloud = AsyncMock()
    acknowledge_frame = AsyncMock()

    manager = SerialHandshakeManager(
        config=config,
        state=state,
        serial_timing=MagicMock(),
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
    with patch("asyncio.wait_for", side_effect=asyncio.TimeoutError):
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
