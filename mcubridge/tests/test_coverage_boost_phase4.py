# pyright: reportPrivateUsage=false
"""Phase 4 SIL-2 Coverage Hardening Test Suite.

Targets 95%+ total project coverage by exercising runtime service lifecycle,
teardown error handlers, datastore paths, handshake synchronization timeouts,
and status report generation.
"""

from __future__ import annotations

import asyncio
import tempfile
from pathlib import Path
from typing import Any, cast
from unittest.mock import AsyncMock, patch

import lmdb
import pytest

import mcubridge.protocol.mcubridge_pb2 as pb
from mcubridge.config.settings import RuntimeConfig
from mcubridge.protocol.protocol import Command, Status, Topic
from mcubridge.protocol.structures import TopicRoute
from mcubridge.services.runtime import BridgeService
from mcubridge.state.context import create_runtime_state
from mcubridge.state.storage import LmdbDeque
from mcubridge.transport.serial import SerialTransport


def _make_config(tmp_path: Path | None = None) -> RuntimeConfig:
    d = str(tmp_path or tempfile.mkdtemp())
    return RuntimeConfig(
        topic_prefix="test/br",
        serial_port="/dev/null",
        serial_baud=115200,
        cloud_spool_dir=d,
        cloud_queue_limit=10,
        allow_non_tmp_paths=True,
    )


def _make_service(config: RuntimeConfig) -> tuple[BridgeService, Any, AsyncMock]:
    state = create_runtime_state(config)
    mock_serial = AsyncMock(spec=SerialTransport)
    mock_serial.send = AsyncMock(return_value=True)
    mock_serial.send_raw = AsyncMock(return_value=True)
    mock_serial.acknowledge = AsyncMock()
    mock_serial.is_open = True
    mock_serial.run = AsyncMock()
    service = BridgeService(config=config, state=state, serial=mock_serial)
    return service, state, mock_serial


@pytest.mark.asyncio
async def test_runtime_service_run_and_teardown_exceptions(tmp_path: Path) -> None:
    config = _make_config(tmp_path)
    config.cloud_enabled = False
    config.watchdog_enabled = True
    config.metrics_enabled = True
    service, state, _ = _make_service(config)

    # Attach mock caches with close exceptions to exercise teardown error handling
    mock_spool = AsyncMock(spec=LmdbDeque)
    mock_spool.close.side_effect = OSError("spool close error")
    service._cloud_spool = mock_spool

    mock_cache = AsyncMock()
    mock_cache.close.side_effect = lmdb.Error("db error")
    state.datastore_cache = mock_cache

    mock_mb_q = AsyncMock(spec=LmdbDeque)
    mock_mb_q.close.side_effect = OSError("mb error")
    state.mailbox_queue = mock_mb_q

    mock_mb_in_q = AsyncMock(spec=LmdbDeque)
    mock_mb_in_q.close.side_effect = OSError("mb in error")
    state.mailbox_incoming_queue = mock_mb_in_q

    # Run service briefly and cancel
    task = asyncio.create_task(service.run())
    await asyncio.sleep(0.02)
    task.cancel()
    try:
        await task
    except asyncio.CancelledError:
        pass

    assert service._cloud_spool is None
    state.cleanup()


@pytest.mark.asyncio
async def test_runtime_run_cloud_disabled(tmp_path: Path) -> None:
    config = _make_config(tmp_path)
    config.cloud_enabled = False
    service, state, _ = _make_service(config)

    await service.run_cloud()
    state.cleanup()


@pytest.mark.asyncio
async def test_runtime_handle_datastore_flavors(tmp_path: Path) -> None:
    config = _make_config(tmp_path)
    service, state, _ = _make_service(config)

    # 1. Datastore PUT
    route_put = TopicRoute(
        raw="test/br/datastore/put/my_key",
        prefix=config.topic_prefix,
        topic=Topic.DATASTORE,
        segments=("put", "my_key"),
    )
    inbound_put = pb.CloudQueuedPublish(topic_name="test/br/datastore/put/my_key", payload=b"my_val")
    await service._handle_datastore(route_put, inbound_put)

    # 2. Datastore GET (cache hit)
    route_get_hit = TopicRoute(
        raw="test/br/datastore/get/my_key",
        prefix=config.topic_prefix,
        topic=Topic.DATASTORE,
        segments=("get", "my_key"),
    )
    inbound_get = pb.CloudQueuedPublish(topic_name="test/br/datastore/get/my_key", payload=b"")
    await service._handle_datastore(route_get_hit, inbound_get)

    # 3. Datastore GET (cache miss with request suffix)
    route_get_miss = TopicRoute(
        raw="test/br/datastore/get/non_existing/request",
        prefix=config.topic_prefix,
        topic=Topic.DATASTORE,
        segments=("get", "non_existing", "request"),
    )
    await service._handle_datastore(route_get_miss, inbound_get)

    state.cleanup()


@pytest.mark.asyncio
async def test_runtime_handle_mcu_status_binary_undecodable(tmp_path: Path) -> None:
    config = _make_config(tmp_path)
    service, state, _ = _make_service(config)

    # Status with invalid UTF-8 and non-protobuf bytes
    await service._handle_mcu_status(Status.TIMEOUT, 1, b"\xff\xfe\xfd\x80")
    # Status with generic object
    await service._handle_mcu_status(Status.ERROR, 2, cast(Any, 12345))

    state.cleanup()


@pytest.mark.asyncio
async def test_handshake_attempt_link_sync_timeout(tmp_path: Path) -> None:
    config = _make_config(tmp_path)
    service, state, _ = _make_service(config)
    handshake = service.handshake

    # Force wait for link sync confirmation to return False
    with (
        patch.object(handshake, "_wait_for_link_sync_confirmation", new_callable=AsyncMock, return_value=False),
        patch.object(handshake, "handle_handshake_failure", new_callable=AsyncMock) as mock_fail,
    ):
        res = await handshake._synchronize_attempt()
        assert res is False
        assert mock_fail.called

    state.cleanup()


@pytest.mark.asyncio
async def test_runtime_local_bridge_subscribe_console(tmp_path: Path) -> None:
    from mcubridge.services.runtime import LocalBridgeService

    config = _make_config(tmp_path)
    service, state, _ = _make_service(config)
    local_service = LocalBridgeService(service)

    mock_stream = AsyncMock()
    mock_stream.recv_message.return_value = pb.SubscribeRequest()
    mock_stream.send_message.side_effect = OSError("client disconnected")

    q: asyncio.Queue[pb.CloudQueuedPublish] = asyncio.Queue()
    q.put_nowait(pb.CloudQueuedPublish(topic_name="console/tx", payload=b"test_console"))

    with patch("asyncio.Queue", return_value=q):
        with pytest.raises(OSError):
            await local_service.SubscribeConsole(mock_stream)

    state.cleanup()


@pytest.mark.asyncio
async def test_runtime_request_mcu_version_and_system_version(tmp_path: Path) -> None:
    config = _make_config(tmp_path)
    service, state, mock_serial = _make_service(config)

    # 1. request_mcu_version with bytes return
    v_resp = pb.VersionResponse(major=2, minor=8, patch=5).SerializeToString()
    mock_serial.send.return_value = v_resp
    inbound = pb.CloudQueuedPublish(topic_name=f"{config.topic_prefix}/system/version/get", payload=b"")

    res = await service._request_mcu_version(inbound)
    assert res is True
    assert state.mcu_version == (2, 8, 5)

    # 2. System Version route
    route_ver = TopicRoute(
        raw="test/br/system/version/get",
        prefix=config.topic_prefix,
        topic=Topic.SYSTEM,
        segments=("version", "get"),
    )
    await service._handle_system(route_ver, inbound)

    state.cleanup()


@pytest.mark.asyncio
async def test_runtime_pin_analog_and_invalid_digits(tmp_path: Path) -> None:
    config = _make_config(tmp_path)
    service, state, mock_serial = _make_service(config)

    # 1. Analog Write
    route_aw = TopicRoute(
        raw="test/br/a/9",
        prefix=config.topic_prefix,
        topic=Topic.ANALOG,
        segments=("9",),
    )
    inbound_aw = pb.CloudQueuedPublish(topic_name="test/br/a/9", payload=b"128")
    await service._handle_pin(route_aw, inbound_aw)
    mock_serial.send.assert_called_with(Command.CMD_ANALOG_WRITE.value, pb.DigitalWrite(pin=9, value=128))

    # 2. Digital Write with non-digit payload (defaults to 0)
    route_dw = TopicRoute(
        raw="test/br/d/13",
        prefix=config.topic_prefix,
        topic=Topic.DIGITAL,
        segments=("13",),
    )
    inbound_dw = pb.CloudQueuedPublish(topic_name="test/br/d/13", payload=b"non_digit")
    await service._handle_pin(route_dw, inbound_dw)
    mock_serial.send.assert_called_with(Command.CMD_DIGITAL_WRITE.value, pb.DigitalWrite(pin=13, value=0))

    state.cleanup()
