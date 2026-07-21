"""Exhaustive gap closure suite 17 for Python daemon SIL-2 coverage (95%+ target)."""

import asyncio
from typing import Any, cast
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
import mcubridge.metrics as metrics_mod
from mcubridge.config.settings import load_runtime_config
from mcubridge.protocol import mcubridge_pb2 as pb
from mcubridge.services.runtime import BridgeService
from mcubridge.state.context import create_runtime_state


@pytest.mark.asyncio
async def test_metrics_emit_exception_handlers():
    cfg = load_runtime_config()
    state = create_runtime_state(cfg)
    emit_bridge = getattr(metrics_mod, "_emit_bridge_snapshot")
    emit_metrics = getattr(metrics_mod, "_emit_metrics_snapshot")

    # 1. _emit_bridge_snapshot with TypeError/AttributeError/OSError
    async def mock_enqueue_bad(msg: Any):
        raise OSError("Enqueue error")

    await emit_bridge(state, mock_enqueue_bad, flavor="summary")

    # 2. _emit_bridge_snapshot with AttributeError from snapshot builder
    with patch.object(state, "build_bridge_snapshot", side_effect=AttributeError("Attribute error")):
        await emit_bridge(state, mock_enqueue_bad, flavor="summary")

    # 3. _emit_metrics_snapshot with watchdog_enabled = True
    state.watchdog_enabled = True
    state.watchdog_interval = 15
    mock_enqueue = AsyncMock()
    await emit_metrics(state, mock_enqueue, expiry_seconds=10.0)
    mock_enqueue.assert_called_once()


@pytest.mark.asyncio
async def test_runtime_spool_pop_and_limit_error_paths():
    cfg = load_runtime_config()
    state = create_runtime_state(cfg)
    mock_serial = AsyncMock()
    service = BridgeService(cfg, state, mock_serial)
    srv = cast(Any, service)

    # 1. _spool_cloud_message_locked with cloud_queue_limit > 0 and spool popleft raising IndexError
    state.cloud_queue_limit = 1
    mock_spool = AsyncMock()
    mock_spool.length.side_effect = [1, 1, 0]
    mock_spool.popleft.side_effect = IndexError("Pop index error")
    mock_spool.append = AsyncMock()
    service._cloud_spool = mock_spool  # type: ignore[reportPrivateUsage]

    from mcubridge.protocol.structures import create_queued_publish

    msg = create_queued_publish("test", b"data")
    res = await srv._spool_cloud_message_locked(msg)
    assert res is True

    # 2. _spool_cloud_message_locked with spool popleft raising OSError
    mock_spool2 = AsyncMock()
    mock_spool2.length.side_effect = [1, 1, 0]
    mock_spool2.popleft.side_effect = OSError("DB error")
    mock_spool2.append = AsyncMock()
    service._cloud_spool = mock_spool2  # type: ignore[reportPrivateUsage]
    await srv._spool_cloud_message_locked(msg)

    # 3. _flush_cloud_spool_locked corrupt entry popleft raising IndexError and OSError
    mock_spool3 = AsyncMock()
    mock_spool3.length.side_effect = [1, 1, 0]
    mock_spool3.popleft.side_effect = IndexError("Corrupt pop index error")
    service._cloud_spool = mock_spool3  # type: ignore[reportPrivateUsage]

    with patch.object(pb.CloudQueuedPublish, "FromString", side_effect=ValueError("Corrupt binary")):
        await service.flush_cloud_spool()

    mock_spool4 = AsyncMock()
    mock_spool4.length.side_effect = [1, 1, 0]
    mock_spool4.popleft.side_effect = OSError("Corrupt pop DB error")
    service._cloud_spool = mock_spool4  # type: ignore[reportPrivateUsage]

    with patch.object(pb.CloudQueuedPublish, "FromString", side_effect=ValueError("Corrupt binary")):
        await service.flush_cloud_spool()


@pytest.mark.asyncio
async def test_runtime_ipc_and_console_routing():
    from mcubridge.protocol.structures import create_queued_publish

    cfg = load_runtime_config()
    state = create_runtime_state(cfg)
    mock_serial = AsyncMock()
    service = BridgeService(cfg, state, mock_serial)

    # 1. enqueue_cloud when correlation is in self.ipc_requests
    q = asyncio.Queue[Any]()
    service.ipc_requests[b"corr123"] = q
    msg = create_queued_publish("test", b"payload")
    reply_ctx = MagicMock(correlation_data=b"corr123")

    with patch.object(service, "_publish_cloud_message", new_callable=AsyncMock):
        await service.enqueue_cloud(msg, reply_context=reply_ctx)
        assert not q.empty()

    # 2. enqueue_cloud with console topic routing to console_queues
    q_console = asyncio.Queue[Any]()
    service.console_queues.append(q_console)
    msg_console = create_queued_publish("bridge/console/out", b"hello")

    with patch.object(service, "_publish_cloud_message", return_value=True):
        await service.enqueue_cloud(msg_console)
        assert not q_console.empty()
