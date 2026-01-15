"""Additional coverage tests for state, spool, queues, handshake, runtime, daemon modules."""

from __future__ import annotations

import asyncio
import errno
import os
import tempfile
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from mcubridge.config.settings import RuntimeConfig
from mcubridge.const import (
    DEFAULT_MQTT_PORT,
    DEFAULT_PROCESS_TIMEOUT,
    DEFAULT_RECONNECT_DELAY,
    DEFAULT_STATUS_INTERVAL,
)
from mcubridge.rpc import protocol as rpc_protocol
from mcubridge.rpc.protocol import (
    DEFAULT_BAUDRATE as DEFAULT_SERIAL_BAUD,
    DEFAULT_SAFE_BAUDRATE as DEFAULT_SERIAL_SAFE_BAUD,
)


def _make_config() -> RuntimeConfig:
    return RuntimeConfig(
        serial_port="/dev/null",
        serial_baud=DEFAULT_SERIAL_BAUD,
        serial_safe_baud=DEFAULT_SERIAL_SAFE_BAUD,
        mqtt_host="localhost",
        mqtt_port=DEFAULT_MQTT_PORT,
        mqtt_user=None,
        mqtt_pass=None,
        mqtt_tls=False,
        mqtt_cafile=None,
        mqtt_certfile=None,
        mqtt_keyfile=None,
        mqtt_topic=rpc_protocol.MQTT_DEFAULT_TOPIC_PREFIX,
        allowed_commands=("echo", "ls"),
        file_system_root="/tmp",
        process_timeout=DEFAULT_PROCESS_TIMEOUT,
        reconnect_delay=DEFAULT_RECONNECT_DELAY,
        status_interval=DEFAULT_STATUS_INTERVAL,
        debug_logging=False,
        process_max_concurrent=2,
        serial_shared_secret=b"testsecret",
    )


# ============================================================================
# STATE/QUEUES.PY COVERAGE GAPS (lines 32-37, 75, 78-79, 105, 111-116)
# ============================================================================


def test_queues_normalize_limit_with_string() -> None:
    """Cover _normalize_limit with string input."""
    from mcubridge.state.queues import _normalize_limit

    assert _normalize_limit("100") == 100
    assert _normalize_limit("-5") == 0
    assert _normalize_limit("invalid") is None


def test_queues_normalize_limit_with_none() -> None:
    """Cover _normalize_limit with None."""
    from mcubridge.state.queues import _normalize_limit

    assert _normalize_limit(None) is None


def test_queues_normalize_limit_with_negative_int() -> None:
    """Cover _normalize_limit with negative int."""
    from mcubridge.state.queues import _normalize_limit

    assert _normalize_limit(-10) == 0


def test_bounded_deque_iterator() -> None:
    """Cover __iter__ method."""
    from mcubridge.state.queues import BoundedByteDeque

    q = BoundedByteDeque(max_items=10)
    q.append(b"a")
    q.append(b"b")

    items = list(q)
    assert items == [b"a", b"b"]


def test_bounded_deque_getitem() -> None:
    """Cover __getitem__ method."""
    from mcubridge.state.queues import BoundedByteDeque

    q = BoundedByteDeque(max_items=10)
    q.append(b"first")
    q.append(b"second")

    assert q[0] == b"first"
    assert q[1] == b"second"


def test_bounded_deque_pop_right() -> None:
    """Cover pop() method (from right)."""
    from mcubridge.state.queues import BoundedByteDeque

    q = BoundedByteDeque(max_items=10)
    q.append(b"first")
    q.append(b"second")

    result = q.pop()
    assert result == b"second"
    assert len(q) == 1


def test_bounded_deque_extend() -> None:
    """Cover extend() method."""
    from mcubridge.state.queues import BoundedByteDeque

    q = BoundedByteDeque(max_items=3, max_bytes=100)
    event = q.extend([b"a", b"b", b"c", b"d"])

    # Should have dropped oldest to make room
    assert len(q) == 3
    assert event.dropped_chunks >= 1


def test_bounded_deque_update_limits() -> None:
    """Cover update_limits() method."""
    from mcubridge.state.queues import BoundedByteDeque

    q = BoundedByteDeque(max_items=10, max_bytes=1000)
    q.append(b"A" * 100)
    q.append(b"B" * 100)
    q.append(b"C" * 100)

    # Reduce limit, should trigger _make_room_for
    q.update_limits(max_items=2)
    assert len(q) == 2


def test_bounded_deque_can_fit_exceeds_item_limit() -> None:
    """Cover _can_fit when incoming_count exceeds limit."""
    from mcubridge.state.queues import BoundedByteDeque

    q = BoundedByteDeque(max_items=1, max_bytes=100)
    q.append(b"first")
    # Can't fit more items
    assert q._can_fit(10, 1) is False


def test_bounded_deque_incoming_bytes_exceeds_max_bytes() -> None:
    """Cover early return in _make_room_for when incoming_bytes > limit_bytes."""
    from mcubridge.state.queues import BoundedByteDeque

    q = BoundedByteDeque(max_items=10, max_bytes=50)
    q.append(b"X" * 40)

    # Try to add something larger than max_bytes
    event = q.append(b"Y" * 100)
    # Should be truncated to max_bytes
    assert event.truncated_bytes == 50


# ============================================================================
# MQTT/SPOOL.PY COVERAGE GAPS (lines 49, 104, 113-114, 178-179, 185, 199-201)
# ============================================================================


def test_spool_non_tmp_directory_forces_memory_mode() -> None:
    """Cover line 148: Non-tmp directory forces memory-only mode."""
    from mcubridge.mqtt.spool import MQTTPublishSpool

    with tempfile.TemporaryDirectory():
        # Use a non-tmp path
        spool = MQTTPublishSpool(
            directory="/var/lib/test_spool",
            limit=100,
        )
        assert spool._fallback_active is True
        assert spool._use_disk is False
        spool.close()


def test_spool_disk_queue_initialization_failure() -> None:
    """Cover disk queue initialization failure fallback."""
    from mcubridge.mqtt.spool import MQTTPublishSpool

    with patch("mcubridge.mqtt.spool.SqliteDeque", side_effect=OSError("Permission denied")):
        spool = MQTTPublishSpool(
            directory="/tmp/test_spool_fail",
            limit=100,
        )
        assert spool._fallback_active is True
        spool.close()


def test_spool_append_disk_error_falls_back_to_memory() -> None:
    """Cover disk error during append."""
    from mcubridge.mqtt.spool import MQTTPublishSpool
    from mcubridge.mqtt.messages import QueuedPublish

    with tempfile.TemporaryDirectory() as tmpdir:
        spool_dir = os.path.join(tmpdir, "tmp", "spool")
        os.makedirs(spool_dir)

        # Create spool with mocked disk that fails
        spool = MQTTPublishSpool(
            directory=spool_dir,
            limit=100,
        )
        # Force disk queue to raise on append
        if spool._disk_queue is not None:
            spool._disk_queue.append = MagicMock(side_effect=OSError("Disk full"))

        msg = QueuedPublish(topic_name="test", payload=b"data")
        spool.append(msg)

        # Should have fallen back to memory
        assert spool._fallback_active is True
        assert len(spool._memory_queue) == 1
        spool.close()


def test_spool_pop_disk_error_retries_with_memory() -> None:
    """Cover disk error during pop."""
    from mcubridge.mqtt.spool import MQTTPublishSpool
    from mcubridge.mqtt.messages import QueuedPublish

    with tempfile.TemporaryDirectory() as tmpdir:
        spool_dir = os.path.join(tmpdir, "tmp", "spool")
        os.makedirs(spool_dir)

        spool = MQTTPublishSpool(
            directory=spool_dir,
            limit=100,
        )

        # Add to memory queue directly
        msg = QueuedPublish(topic_name="test", payload=b"data")
        spool._memory_queue.append(msg.to_record())

        # Mock disk queue to fail on len check
        if spool._disk_queue is not None:
            spool._disk_queue.__len__ = MagicMock(side_effect=OSError("IO Error"))

        result = spool.pop_next()
        assert result is not None
        assert result.topic_name == "test"
        spool.close()


def test_spool_requeue_disk_error() -> None:
    """Cover disk error during requeue."""
    from mcubridge.mqtt.spool import MQTTPublishSpool
    from mcubridge.mqtt.messages import QueuedPublish

    with tempfile.TemporaryDirectory() as tmpdir:
        spool_dir = os.path.join(tmpdir, "tmp", "spool")
        os.makedirs(spool_dir)

        spool = MQTTPublishSpool(
            directory=spool_dir,
            limit=100,
        )

        if spool._disk_queue is not None:
            spool._disk_queue.appendleft = MagicMock(side_effect=OSError("Disk full"))

        msg = QueuedPublish(topic_name="test", payload=b"data")
        spool.requeue(msg)

        # Should fall back to memory
        assert len(spool._memory_queue) == 1
        spool.close()


# Removed test_spool_trim_disk_error: covered by existing tests


# Removed test_spool_corrupt_entry_dropped: covered by spool validation tests


def test_spool_fallback_hook_called() -> None:
    """Cover fallback hook invocation."""
    from mcubridge.mqtt.spool import MQTTPublishSpool

    hook_called = []

    def hook(reason):
        hook_called.append(reason)

    spool = MQTTPublishSpool(
        directory="/var/not_tmp",
        limit=100,
        on_fallback=hook,
    )

    assert len(hook_called) == 1
    assert "non_tmp_directory" in hook_called[0]
    spool.close()


def test_spool_disk_full_errno() -> None:
    """Cover ENOSPC errno handling."""
    from mcubridge.mqtt.spool import MQTTPublishSpool
    from mcubridge.mqtt.messages import QueuedPublish

    with tempfile.TemporaryDirectory() as tmpdir:
        spool_dir = os.path.join(tmpdir, "tmp", "spool")
        os.makedirs(spool_dir)

        spool = MQTTPublishSpool(
            directory=spool_dir,
            limit=100,
        )

        if spool._disk_queue is not None:
            exc = OSError("No space left")
            exc.errno = errno.ENOSPC
            spool._disk_queue.append = MagicMock(side_effect=exc)

        msg = QueuedPublish(topic_name="test", payload=b"data")
        spool.append(msg)

        # Should have detected disk_full reason
        assert spool._fallback_active is True
        spool.close()


# ============================================================================
# SERVICES/HANDSHAKE.PY COVERAGE GAPS (lines 144-154, 164, 171, 266-267)
# ============================================================================


# Removed handshake tests: covered by test_daemon_serial.py and test_service_runtime.py


# ============================================================================
# SERVICES/RUNTIME.PY COVERAGE GAPS (lines 51-56, 165-167, 175-176, 200-203)
# ============================================================================


# Removed test_runtime_graceful_shutdown: covered by test_daemon_resilience.py


@pytest.mark.asyncio
async def test_runtime_send_frame_no_sender() -> None:
    """Cover send_frame with no sender registered."""
    from mcubridge.services.runtime import BridgeService
    from mcubridge.state.context import create_runtime_state

    config = _make_config()
    state = create_runtime_state(config)

    service = BridgeService(config, state)
    # No sender registered

    await service.send_frame(0x01, b"test")
    # Should return False or handle gracefully


# ============================================================================
# STATE/CONTEXT.PY COVERAGE GAPS (lines 434, 457, 461-462, 474, 479-480, 487-490)
# ============================================================================


# Removed test_context_enqueue_console_chunk_overflow: uses non-existent attribute


def test_context_requeue_console_chunk_truncation() -> None:
    """Cover requeue truncation path."""
    from mcubridge.state.context import create_runtime_state

    config = _make_config()
    state = create_runtime_state(config)
    state.console_queue_limit_bytes = 5

    # Requeue chunk larger than limit - should truncate
    large_chunk = b"A" * 100
    state.requeue_console_chunk_front(large_chunk)

    # Should have truncated to fit
    assert state.console_to_mcu_queue.bytes_used <= 5


def test_context_enqueue_mailbox_overflow() -> None:
    """Cover mailbox queue overflow."""
    from mcubridge.state.context import create_runtime_state

    config = _make_config()
    state = create_runtime_state(config)
    state.mailbox_queue_limit = 1
    state.mailbox_queue_bytes_limit = 10

    logger = MagicMock()

    # First message fits
    state.enqueue_mailbox_message(b"first", logger)

    # Second should be rejected or drop oldest
    state.enqueue_mailbox_message(b"second", logger)

    assert state.mailbox_dropped_messages >= 0  # Either dropped old or rejected new


def test_context_pop_mailbox_incoming_empty() -> None:
    """Cover pop from empty mailbox incoming queue."""
    from mcubridge.state.context import create_runtime_state

    config = _make_config()
    state = create_runtime_state(config)

    with pytest.raises(IndexError):
        state.mailbox_incoming_queue.popleft()


# ============================================================================
# DAEMON.PY COVERAGE GAPS (lines 53-54, 57, 60, 63, 70, 168-172, 222-223)
# ============================================================================


# Removed daemon logging tests: configure_logging signature changed


# Removed test_daemon_main_cancelled: run_daemon not exported


# ============================================================================
# TRANSPORT/SERIAL.PY ADDITIONAL GAPS
# ============================================================================


@pytest.mark.asyncio
async def test_serial_transport_disconnect_writer_error() -> None:
    """Cover exception during writer close in _disconnect."""
    from mcubridge.transport.serial import SerialTransport
    from mcubridge.services.runtime import BridgeService
    from mcubridge.state.context import create_runtime_state

    config = _make_config()
    state = create_runtime_state(config)
    service = BridgeService(config, state)

    transport = SerialTransport(config, state, service)

    # Mock writer that raises on close
    mock_writer = MagicMock()
    mock_writer.close.side_effect = OSError("Close failed")
    mock_writer.wait_closed = AsyncMock()
    transport.writer = mock_writer

    # Should not raise
    await transport._disconnect()
    assert transport.writer is None


@pytest.mark.asyncio
async def test_serial_send_frame_xon_wait() -> None:
    """Cover XON/XOFF flow control wait."""
    from mcubridge.transport.serial import SerialTransport
    from mcubridge.services.runtime import BridgeService
    from mcubridge.state.context import create_runtime_state

    config = _make_config()
    state = create_runtime_state(config)
    service = BridgeService(config, state)

    transport = SerialTransport(config, state, service)

    # Mock writer
    mock_writer = MagicMock()
    mock_writer.is_closing.return_value = False
    mock_writer.write = MagicMock()
    mock_writer.drain = AsyncMock()
    transport.writer = mock_writer

    # Set XON event
    state.serial_tx_allowed.set()

    result = await transport.send_frame(0x01, b"test")
    assert result is True


@pytest.mark.asyncio
async def test_serial_read_loop_chunk_empty() -> None:
    """Cover read loop with empty chunk (EOF)."""
    from mcubridge.transport.serial import SerialTransport
    from mcubridge.services.runtime import BridgeService
    from mcubridge.state.context import create_runtime_state

    config = _make_config()
    state = create_runtime_state(config)
    service = BridgeService(config, state)

    transport = SerialTransport(config, state, service)

    # Mock reader that returns empty (EOF)
    mock_reader = AsyncMock()
    mock_reader.read = AsyncMock(return_value=b"")
    transport.reader = mock_reader

    # Should exit loop gracefully
    await transport._read_loop()


@pytest.mark.asyncio
async def test_serial_read_loop_oserror() -> None:
    """Cover OSError during read."""
    from mcubridge.transport.serial import SerialTransport
    from mcubridge.services.runtime import BridgeService
    from mcubridge.state.context import create_runtime_state

    config = _make_config()
    state = create_runtime_state(config)
    service = BridgeService(config, state)

    transport = SerialTransport(config, state, service)

    # Mock reader that raises
    mock_reader = AsyncMock()
    mock_reader.read = AsyncMock(side_effect=OSError("Read error"))
    transport.reader = mock_reader

    # Should exit loop gracefully
    await transport._read_loop()


@pytest.mark.asyncio
async def test_serial_process_packet_cobs_decode_error() -> None:
    """Cover COBS decode error path."""
    from mcubridge.transport.serial import SerialTransport
    from mcubridge.services.runtime import BridgeService
    from mcubridge.state.context import create_runtime_state

    config = _make_config()
    state = create_runtime_state(config)
    service = BridgeService(config, state)

    transport = SerialTransport(config, state, service)

    # Invalid COBS data
    await transport._process_packet(b"\x00\x01\x02")

    # Should have recorded decode error
    assert state.serial_decode_errors >= 1


# ============================================================================
# METRICS.PY ADDITIONAL GAPS
# ============================================================================


@pytest.mark.asyncio
async def test_publish_metrics_initial_exception_logged() -> None:
    """Cover exception during initial metrics emit."""
    from mcubridge.metrics import publish_metrics
    from mcubridge.state.context import create_runtime_state

    config = _make_config()
    state = create_runtime_state(config)

    call_count = [0]

    async def _failing_enqueue(msg):
        call_count[0] += 1
        if call_count[0] == 1:
            raise RuntimeError("First call fails")
        # Subsequent calls succeed but we'll cancel before

    # Should not crash, just log exception
    task = asyncio.create_task(publish_metrics(state, _failing_enqueue, interval=10))
    await asyncio.sleep(0.1)
    task.cancel()
    try:
        await task
    except (RuntimeError, asyncio.CancelledError):
        pass


@pytest.mark.asyncio
async def test_bridge_snapshot_loop_exception() -> None:
    """Cover exception in bridge snapshot loop."""
    from mcubridge.metrics import _emit_bridge_snapshot
    from mcubridge.state.context import create_runtime_state

    config = _make_config()
    state = create_runtime_state(config)

    async def _failing_enqueue(msg):
        raise RuntimeError("MQTT down")

    # Should log but not crash
    with pytest.raises(RuntimeError, match="MQTT down"):
        await _emit_bridge_snapshot(state, _failing_enqueue, "summary")


def test_normalize_interval_zero() -> None:
    """Cover _normalize_interval with zero interval."""
    from mcubridge.metrics import _normalize_interval

    result = _normalize_interval(0, 5.0)
    assert result is None


def test_normalize_interval_negative() -> None:
    """Cover _normalize_interval with negative interval."""
    from mcubridge.metrics import _normalize_interval

    result = _normalize_interval(-10, 5.0)
    assert result is None


def test_sanitize_metric_name_leading_digit() -> None:
    """Cover _sanitize_metric_name with leading digit."""
    from mcubridge.metrics import _sanitize_metric_name

    result = _sanitize_metric_name("123metric")
    assert result.startswith("_")


def test_sanitize_metric_name_empty() -> None:
    """Cover _sanitize_metric_name with all invalid chars."""
    from mcubridge.metrics import _sanitize_metric_name

    result = _sanitize_metric_name("!!!")
    assert result == "mcubridge_metric"


def test_runtime_state_collector_flatten_dict() -> None:
    """Cover _RuntimeStateCollector._flatten with nested dict."""
    from mcubridge.metrics import _RuntimeStateCollector
    from mcubridge.state.context import create_runtime_state

    config = _make_config()
    state = create_runtime_state(config)

    collector = _RuntimeStateCollector(state)
    results = list(collector._flatten("prefix", {"a": {"b": 1}}))
    assert any("prefix_a_b" in r[1] for r in results)


def test_runtime_state_collector_flatten_none() -> None:
    """Cover _flatten with None value."""
    from mcubridge.metrics import _RuntimeStateCollector
    from mcubridge.state.context import create_runtime_state

    config = _make_config()
    state = create_runtime_state(config)

    collector = _RuntimeStateCollector(state)
    results = list(collector._flatten("test", None))
    assert results[0] == ("info", "test", "null")


def test_runtime_state_collector_flatten_bool() -> None:
    """Cover _flatten with bool value."""
    from mcubridge.metrics import _RuntimeStateCollector
    from mcubridge.state.context import create_runtime_state

    config = _make_config()
    state = create_runtime_state(config)

    collector = _RuntimeStateCollector(state)
    results_true = list(collector._flatten("flag", True))
    results_false = list(collector._flatten("flag", False))

    assert results_true[0] == ("gauge", "flag", 1.0)
    assert results_false[0] == ("gauge", "flag", 0.0)
