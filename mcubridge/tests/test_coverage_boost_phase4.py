"""Phase 4 SIL-2 Coverage Hardening Test Suite.

Targets 95%+ total project coverage by exercising runtime service lifecycle,
teardown error handlers, datastore paths, handshake synchronization timeouts,
and status report generation.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
import asyncio
from pathlib import Path
import tempfile
from typing import Any, cast
from unittest.mock import AsyncMock

from hypothesis import given, settings, strategies as st
import lmdb
import pytest
from pytest_mock import MockerFixture
import structlog

from mcubridge.config.settings import RuntimeConfig
import mcubridge.protocol.mcubridge_pb2 as pb
from mcubridge.protocol.protocol import Command, Status, Topic
from mcubridge.protocol.structures import TopicRoute
from mcubridge.services.runtime import BridgeService
from mcubridge.state.context import create_runtime_state
from mcubridge.state.storage import LmdbDeque
from mcubridge.transport.serial import SerialTransport

logger = structlog.get_logger(__name__)


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
    mock_serial.is_open = True
    service = BridgeService(config, state, mock_serial)
    return service, state, mock_serial


# ══════════════════════════════════════════════════════════════════════════════
# 1. Runtime Service Lifecycle & Teardown Exception Paths
# ══════════════════════════════════════════════════════════════════════════════


@pytest.mark.asyncio
async def test_runtime_service_run_and_teardown_exceptions(tmp_path: Path) -> None:
    config = _make_config(tmp_path)
    service, state, _ = _make_service(config)

    # Attach mock caches with close exceptions to exercise teardown error handling
    mock_spool = AsyncMock(spec=LmdbDeque)
    mock_spool.close.side_effect = OSError("spool close error")
    setattr(service, "_cloud_spool", mock_spool)

    mock_cache = AsyncMock()
    mock_cache.close.side_effect = lmdb.Error("db error")
    state.datastore_cache = mock_cache

    run_task = asyncio.create_task(service.run())
    await asyncio.sleep(0.02)
    run_task.cancel()

    try:
        await run_task
    except asyncio.CancelledError as exc:
        logger.debug("Service run task cancelled as expected", error=str(exc))

    assert getattr(service, "_cloud_spool") is None
    state.cleanup()


@pytest.mark.asyncio
async def test_runtime_run_cloud_disabled(tmp_path: Path, mocker: MockerFixture) -> None:
    config = _make_config(tmp_path)
    config.cloud_enabled = False
    service, state, _ = _make_service(config)

    mock_info = mocker.patch("mcubridge.services.runtime.logger.info")
    await service.run_cloud()
    assert mock_info.called
    state.cleanup()


@settings(max_examples=25, derandomize=True, deadline=None)
@given(
    key=st.text(alphabet="abcdefghijklmnopqrstuvwxyz0123456789_", min_size=1, max_size=24),
    value=st.binary(min_size=1, max_size=128),
)
def test_runtime_handle_datastore_flavors(
    tmp_path_factory: pytest.TempPathFactory, key: str, value: bytes
) -> None:
    async def _run() -> None:
        config = _make_config(Path(tmp_path_factory.mktemp("datastore_flavors")))
        service, state, _ = _make_service(config)

        # 1. Datastore PUT
        route_put = TopicRoute(
            raw=f"{config.topic_prefix}/datastore/put/{key}",
            prefix=config.topic_prefix,
            topic=Topic.DATASTORE,
            segments=("put", key),
        )
        handle_datastore: Callable[[TopicRoute, pb.CloudQueuedPublish], Awaitable[None]] = getattr(
            service, "_handle_datastore"
        )
        inbound_put = pb.CloudQueuedPublish(topic_name=f"{config.topic_prefix}/datastore/put/{key}", payload=value)
        await handle_datastore(route_put, inbound_put)
        assert await state.datastore_cache.get(key) == value

        # 2. Datastore GET (cache hit)
        route_get_hit = TopicRoute(
            raw=f"{config.topic_prefix}/datastore/get/{key}",
            prefix=config.topic_prefix,
            topic=Topic.DATASTORE,
            segments=("get", key),
        )
        inbound_get = pb.CloudQueuedPublish(topic_name=f"{config.topic_prefix}/datastore/get/{key}", payload=b"")
        mock_enqueue = AsyncMock()
        service.enqueue_cloud = mock_enqueue
        await handle_datastore(route_get_hit, inbound_get)
        assert mock_enqueue.await_count == 1

        # 3. Datastore GET (cache miss with request suffix)
        route_get_miss = TopicRoute(
            raw=f"{config.topic_prefix}/datastore/get/non_existing_{key}/request",
            prefix=config.topic_prefix,
            topic=Topic.DATASTORE,
            segments=("get", f"non_existing_{key}", "request"),
        )
        mock_enqueue.reset_mock()
        await handle_datastore(route_get_miss, inbound_get)
        assert mock_enqueue.await_count == 1

        state.cleanup()

    asyncio.run(_run())


@pytest.mark.asyncio
async def test_runtime_handle_mcu_status_binary_undecodable(tmp_path: Path, mocker: MockerFixture) -> None:
    config = _make_config(tmp_path)
    service, state, _ = _make_service(config)

    mock_enqueue = mocker.patch.object(service, "enqueue_cloud", new_callable=AsyncMock)
    handle_mcu_status: Callable[..., Awaitable[None]] = getattr(service, "_handle_mcu_status")
    # Status with invalid UTF-8 and non-protobuf bytes
    await handle_mcu_status(Status.TIMEOUT, 1, b"\xff\xfe\xfd\x80")
    assert mock_enqueue.call_count == 1
    # Status with generic object
    await handle_mcu_status(Status.ERROR, 2, cast(Any, 12345))
    assert mock_enqueue.call_count == 2

    state.cleanup()


# ══════════════════════════════════════════════════════════════════════════════
# 2. Handshake & Link Sync Timeout Handling
# ══════════════════════════════════════════════════════════════════════════════


@pytest.mark.asyncio
async def test_handshake_attempt_link_sync_timeout(tmp_path: Path, mocker: MockerFixture) -> None:
    config = _make_config(tmp_path)
    service, state, _ = _make_service(config)
    handshake = service.handshake

    # Force wait for link sync confirmation to return False
    mocker.patch.object(handshake, "_wait_for_link_sync_confirmation", new_callable=AsyncMock, return_value=False)
    mock_fail = mocker.patch.object(handshake, "handle_handshake_failure", new_callable=AsyncMock)

    sync_attempt: Callable[[], Awaitable[bool]] = getattr(handshake, "_synchronize_attempt")
    res = await sync_attempt()
    assert res is False
    assert mock_fail.called

    state.cleanup()


# ══════════════════════════════════════════════════════════════════════════════
# 3. Local Bridge Console Subscription & System Version Request Edge Paths
# ══════════════════════════════════════════════════════════════════════════════


@pytest.mark.asyncio
async def test_runtime_local_bridge_subscribe_console(tmp_path: Path, mocker: MockerFixture) -> None:
    from mcubridge.services.runtime import LocalBridgeService

    config = _make_config(tmp_path)
    service, state, _ = _make_service(config)
    local_service = LocalBridgeService(service)

    mock_stream = AsyncMock()
    mock_stream.send_message.side_effect = OSError("Connection aborted")

    q: asyncio.Queue[pb.CloudQueuedPublish] = asyncio.Queue()
    q.put_nowait(pb.CloudQueuedPublish(topic_name="console/tx", payload=b"test_console"))

    mocker.patch("asyncio.Queue", return_value=q)
    with pytest.raises(OSError):
        await local_service.SubscribeConsole(mock_stream)

    state.cleanup()


@pytest.mark.asyncio
async def test_runtime_request_mcu_version_and_system_version(tmp_path: Path) -> None:
    config = _make_config(tmp_path)
    service, state, mock_serial = _make_service(config)

    v_resp = pb.VersionResponse(major=2, minor=8, patch=5).SerializeToString()
    mock_serial.send.return_value = v_resp
    inbound = pb.CloudQueuedPublish(topic_name=f"{config.topic_prefix}/system/version/get", payload=b"")

    req_mcu_version: Callable[[pb.CloudQueuedPublish], Awaitable[bool]] = getattr(service, "_request_mcu_version")
    res = await req_mcu_version(inbound)
    assert res is True
    assert state.mcu_version == (2, 8, 5)

    # Trigger system version dispatch
    route_ver = TopicRoute(
        raw=f"{config.topic_prefix}/system/version/get",
        prefix=config.topic_prefix,
        topic=Topic.SYSTEM,
        segments=("version", "get"),
    )
    handle_system: Callable[[TopicRoute, pb.CloudQueuedPublish], Awaitable[None]] = getattr(service, "_handle_system")
    await handle_system(route_ver, inbound)

    state.cleanup()


@settings(max_examples=25, derandomize=True, deadline=None)
@given(
    pin=st.integers(0, 32),
    analog_val=st.integers(0, 255),
    non_digit=st.text(alphabet="abcdefghijklmnopqrstuvwxyz!@#$", min_size=1, max_size=12),
)
def test_runtime_pin_analog_and_invalid_digits(
    tmp_path_factory: pytest.TempPathFactory, pin: int, analog_val: int, non_digit: str
) -> None:
    async def _run() -> None:
        config = _make_config(Path(tmp_path_factory.mktemp("pin_analog")))
        service, state, mock_serial = _make_service(config)

        handle_pin: Callable[[TopicRoute, pb.CloudQueuedPublish], Awaitable[None]] = getattr(service, "_handle_pin")

        # 1. Analog Write
        route_aw = TopicRoute(
            raw=f"{config.topic_prefix}/a/{pin}",
            prefix=config.topic_prefix,
            topic=Topic.ANALOG,
            segments=(str(pin),),
        )
        inbound_aw = pb.CloudQueuedPublish(topic_name=f"{config.topic_prefix}/a/{pin}", payload=str(analog_val).encode())
        await handle_pin(route_aw, inbound_aw)
        mock_serial.send.assert_called_with(Command.CMD_ANALOG_WRITE.value, pb.AnalogWrite(pin=pin, value=analog_val))

        # 2. Digital Write with non-digit payload (defaults to 0)
        route_dw = TopicRoute(
            raw=f"{config.topic_prefix}/d/{pin}",
            prefix=config.topic_prefix,
            topic=Topic.DIGITAL,
            segments=(str(pin),),
        )
        inbound_dw = pb.CloudQueuedPublish(topic_name=f"{config.topic_prefix}/d/{pin}", payload=non_digit.encode())
        await handle_pin(route_dw, inbound_dw)
        mock_serial.send.assert_called_with(Command.CMD_DIGITAL_WRITE.value, pb.DigitalWrite(pin=pin, value=0))

        state.cleanup()

    asyncio.run(_run())
