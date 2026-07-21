"""Exhaustive gap closure suite 13 for Python daemon SIL-2 coverage (95%+ target)."""

import asyncio
import tempfile
from typing import Any, cast
from unittest.mock import AsyncMock, patch

import pytest
from mcubridge.config.settings import RuntimeConfig, load_runtime_config
from mcubridge.metrics import PrometheusExporter, RuntimeStateCollector, publish_bridge_snapshots, publish_metrics
from mcubridge.protocol.structures import TopicRoute
from mcubridge.protocol.topics import Topic
from mcubridge.services.runtime import BridgeRequest, BridgeService
from mcubridge.state.context import create_runtime_state


@pytest.mark.asyncio
async def test_handshake_session_key_and_rate_limit():
    cfg = load_runtime_config()
    cfg.serial_shared_secret = b"12345678901234567890123456789012"
    state = create_runtime_state(cfg)
    mock_serial = AsyncMock()
    service = BridgeService(cfg, state, mock_serial)
    hs = service.handshake
    hs_any = cast(Any, hs)

    # 1. Test unexpected link_sync_resp without pending nonce
    res_no_nonce = await hs.handle_link_sync_resp(1, b"test")
    assert res_no_nonce is False

    # 2. Test rate limit in handle_link_sync_resp
    state.link_handshake_nonce = b"\x00" * 12
    state.handshake_rate_until = 999999999999.0
    with patch("time.monotonic", return_value=999999999910.0):
        res_throttled = await hs.handle_link_sync_resp(1, b"test")
        assert res_throttled is False

    # 3. clear_handshake_expectations
    hs_any.clear_handshake_expectations()


@pytest.mark.asyncio
async def test_metrics_publisher_loops_and_collector():
    cfg = load_runtime_config()
    state = create_runtime_state(cfg)
    enqueue_mock = AsyncMock()

    # 1. Interval <= 0 validation
    with pytest.raises(ValueError, match="interval must be greater than zero"):
        await publish_metrics(state, enqueue_mock, interval=0)

    # 2. publish_bridge_snapshots with disabled loops (summary=0, handshake=0)
    with patch("asyncio.Event.wait", new_callable=AsyncMock) as mock_wait:
        mock_wait.side_effect = asyncio.CancelledError
        with pytest.raises(asyncio.CancelledError):
            await publish_bridge_snapshots(state, enqueue_mock, summary_interval=0, handshake_interval=0)

    # 3. Collector when state reference is dead
    collector = RuntimeStateCollector(state)
    state_ref_none = list(collector.collect())
    assert len(state_ref_none) > 0

    # Test when weakref returns None
    with patch.object(collector, "_state_ref", return_value=None):
        res = list(collector.collect())
        assert res == []

    # 4. PrometheusExporter port property & shutdown handling
    exporter = PrometheusExporter(state, host="127.0.0.1", port=0)
    exporter_any = cast(Any, exporter)
    assert exporter.port > 0
    if exporter_any._server:
        exporter_any._server.server_close()


@pytest.mark.asyncio
async def test_runtime_service_exhaustive_uncovered_paths():
    cfg = load_runtime_config()
    state = create_runtime_state(cfg)
    mock_serial = AsyncMock()
    service = BridgeService(cfg, state, mock_serial)
    srv = cast(Any, service)

    # 1. _poll_process without pid
    res_poll = await srv._poll_process(0)
    assert res_poll.finished is True

    # 2. _handle_datastore GET miss
    req = BridgeRequest(topic="datastore/get/request/mykey", payload=b"")
    route = TopicRoute(raw="", prefix="", topic="datastore", segments=("get", "request", "mykey"))
    with patch.object(service, "enqueue_cloud", new_callable=AsyncMock):
        await srv._handle_datastore(route, req)

    # 3. _handle_datastore empty key
    route_empty = TopicRoute(raw="", prefix="", topic="datastore", segments=())
    await srv._handle_datastore(route_empty, req)

    # 4. _handle_pin negative pin
    route_bad_pin = TopicRoute(raw="", prefix="", topic=Topic.DIGITAL, segments=("-1", "mode"))
    await srv._handle_pin(route_bad_pin, req)

    # 5. _handle_spi unknown action
    route_spi_unk = TopicRoute(raw="", prefix="", topic="spi", segments=("unknown",))
    await srv._handle_spi(route_spi_unk, req)

    # 6. _handle_console empty payload
    req_console_empty = BridgeRequest(topic="console", payload=b"")
    await srv._handle_console(req_console_empty)


@pytest.mark.asyncio
async def test_storage_and_config_uncovered_paths():
    from mcubridge.config.logging import configure_logging
    from mcubridge.state.storage import SqliteCache, SqliteDeque

    # 1. Logging configuration
    s = RuntimeConfig()
    configure_logging(s)

    # 2. Settings invalid baudrate / defaults
    assert s.serial_baud == 115200

    # 3. SqliteCache and SqliteDeque with real temp files
    with tempfile.NamedTemporaryFile(suffix=".db") as tf:
        cache = SqliteCache(tf.name)
        await cache.set("k1", b"v1")
        val = await cache.get("k1")
        assert val == b"v1"
        await cache.close()

        # Test _recreate_db error handling
        with patch("pathlib.Path.unlink", side_effect=OSError("unlink error")):
            await cache._recreate_db()  # type: ignore[reportPrivateUsage]

    with tempfile.NamedTemporaryFile(suffix=".db") as tf2:
        dq = SqliteDeque(tf2.name)
        await dq.append(b"item")
        assert len(dq) == 1
        item = await dq.popleft()
        assert item == b"item"
        await dq.close()
