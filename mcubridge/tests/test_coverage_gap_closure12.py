from typing import Any, cast
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from mcubridge.config.settings import load_runtime_config
from mcubridge.protocol import mcubridge_pb2 as pb
from mcubridge.services.runtime import BridgeService
from mcubridge.state.context import create_runtime_state


@pytest.mark.asyncio
async def test_spool_limit_exceptions():
    cfg = load_runtime_config()
    cfg.cloud_queue_limit = 1
    state = create_runtime_state(cfg)
    mock_serial = AsyncMock()

    service = BridgeService(cfg, state, mock_serial)
    srv = cast(Any, service)

    # 1. IndexError on popleft during limit trim
    mock_spool = AsyncMock()
    mock_spool.length.side_effect = [2, 0]
    mock_spool.popleft.side_effect = IndexError("Empty deque")
    srv._cloud_spool = mock_spool

    msg = pb.CloudQueuedPublish(topic_name="test/topic", payload=b"data")
    res = await srv._spool_cloud_message_locked(msg)
    assert res is True

    # 2. OSError on popleft during limit trim
    mock_spool = AsyncMock()
    mock_spool.length.side_effect = [2, 0]
    mock_spool.popleft.side_effect = OSError("DB lock")
    srv._cloud_spool = mock_spool

    res = await srv._spool_cloud_message_locked(msg)
    assert res is True


@pytest.mark.asyncio
async def test_flush_spool_exceptions():
    cfg = load_runtime_config()
    state = create_runtime_state(cfg)
    mock_serial = AsyncMock()

    service = BridgeService(cfg, state, mock_serial)
    srv = cast(Any, service)
    srv._cloud_stream = AsyncMock()

    # 1. length() OSError
    mock_spool = AsyncMock()
    mock_spool.length.side_effect = OSError("DB error")
    srv._cloud_spool = mock_spool

    await srv._flush_cloud_spool_locked()
    assert service.state.cloud_spool_degraded is True

    # 2. popleft IndexError after successful publish
    mock_spool = AsyncMock()
    msg = pb.CloudQueuedPublish(topic_name="test/topic", payload=b"data")
    mock_spool.length.side_effect = [1, 0]
    mock_spool.peek.return_value = msg.SerializeToString()
    mock_spool.popleft.side_effect = IndexError("Empty pop")
    srv._cloud_spool = mock_spool

    with patch.object(service, "_publish_cloud_message", new_callable=AsyncMock, return_value=True):
        await srv._flush_cloud_spool_locked()

    # 3. popleft OSError after successful publish
    mock_spool = AsyncMock()
    mock_spool.length.side_effect = [1, 0]
    mock_spool.peek.return_value = msg.SerializeToString()
    mock_spool.popleft.side_effect = OSError("DB error")
    srv._cloud_spool = mock_spool

    with patch.object(service, "_publish_cloud_message", new_callable=AsyncMock, return_value=True):
        await srv._flush_cloud_spool_locked()
    assert service.state.cloud_spool_degraded is True


@pytest.mark.asyncio
async def test_runtime_service_extra_paths():
    cfg = load_runtime_config()
    state = create_runtime_state(cfg)
    mock_serial = MagicMock()
    service = BridgeService(cfg, state, mock_serial)
    srv = cast(Any, service)
    service.serial = None  # type: ignore[assignment]

    # _request_mcu_version when serial is None
    res = await srv._request_mcu_version()
    assert res is False

    # _flush_console_queue when queue empty
    await srv._flush_console_queue()

    # _poll_process when process not found
    resp = await srv._poll_process(99999)
    assert resp.exit_code == 1
    assert resp.finished is True
