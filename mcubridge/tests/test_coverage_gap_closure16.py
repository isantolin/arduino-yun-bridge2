"""Exhaustive gap closure suite 16 for Python daemon SIL-2 coverage (95%+ target)."""

import asyncio
import tempfile
from typing import Any, cast
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from mcubridge.config.settings import load_runtime_config
from mcubridge.protocol import mcubridge_pb2 as pb
from mcubridge.protocol import protocol
from mcubridge.services.runtime import BridgeRequest, BridgeService
from mcubridge.state.context import create_runtime_state


@pytest.mark.asyncio
async def test_client_console_listener_and_disconnect():
    from mcubridge_client import Bridge

    # 1. disconnect when channel is set
    client = Bridge(socket_path="/tmp/nonexistent.sock")
    client.channel = MagicMock()
    await client.disconnect()
    assert client.channel is None

    # 2. _console_listener with empty payload message
    mock_msg = MagicMock()
    mock_msg.payload = b""

    class MockStream:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            pass

        async def send_message(self, msg):
            pass

        def __aiter__(self):
            return self

        async def __anext__(self):
            if not hasattr(self, "_sent"):
                self._sent = True
                return mock_msg
            raise StopAsyncIteration

    client.stub = MagicMock()
    client.stub.SubscribeConsole.open.return_value = MockStream()
    await client._console_listener()  # type: ignore[reportPrivateUsage]
    val = client._console_queue.get_nowait()  # type: ignore[reportPrivateUsage]
    assert val == b""


@pytest.mark.asyncio
async def test_system_and_mcu_status_formatting():
    from mcubridge.protocol.structures import TopicRoute
    cfg = load_runtime_config()
    state = create_runtime_state(cfg)
    mock_serial = AsyncMock()
    service = BridgeService(cfg, state, mock_serial)
    srv = cast(Any, service)

    # 1. _handle_mcu_status with un-decodable bytes payload
    with patch.object(service, "enqueue_cloud", new_callable=AsyncMock):
        await srv._handle_mcu_status(protocol.Status.ERROR, 1, b"\xff\xfe\xfd")

    # 2. _handle_system FreeMemory & Version
    mock_serial.send.return_value = pb.FreeMemoryResponse(value=1024).SerializeToString()
    route_mem = TopicRoute(raw="", prefix="", topic="system", segments=("free_memory", "get"))
    req = BridgeRequest(topic="system/free_memory/get", payload=b"")
    with patch.object(service, "enqueue_cloud", new_callable=AsyncMock):
        await srv._handle_system(route_mem, req)

    mock_serial.send.return_value = pb.VersionResponse(major=2, minor=8, patch=5).SerializeToString()
    route_ver = TopicRoute(raw="", prefix="", topic="system", segments=("version", "get"))
    with patch.object(service, "enqueue_cloud", new_callable=AsyncMock):
        await srv._handle_system(route_ver, req)


@pytest.mark.asyncio
async def test_serial_correlate_frame_branches():
    from mcubridge.transport.serial import SerialTransport
    cfg = load_runtime_config()
    state = create_runtime_state(cfg)
    mock_serial = AsyncMock()
    st = SerialTransport(cfg, state, mock_serial)
    st_any = cast(Any, st)

    # 1. _correlate_frame with failure status code
    m_pending = MagicMock()
    m_pending.command_id = protocol.Command.CMD_DIGITAL_WRITE.value
    m_pending.success = None
    st_any._current = m_pending

    st_any._correlate_frame(protocol.Status.ERROR.value, b"error")
    m_pending.mark_failure.assert_called_with(protocol.Status.ERROR.value)

    # 2. _correlate_frame with success status code
    m_pending2 = MagicMock()
    m_pending2.command_id = protocol.Command.CMD_DIGITAL_WRITE.value
    m_pending2.success = None
    m_pending2.expected_resp_ids = ()
    st_any._current = m_pending2

    st_any._correlate_frame(protocol.Status.OK.value, b"ok")
    m_pending2.mark_success.assert_called_with(b"ok")


@pytest.mark.asyncio
async def test_storage_db_error_paths():
    from mcubridge.state.storage import SqliteCache, SqliteDeque

    async def raising_connect(*args, **kwargs):
        raise OSError("DB error")

    with tempfile.NamedTemporaryFile(suffix=".db") as tf:
        cache = SqliteCache(tf.name)
        with patch("aiosqlite.connect", side_effect=raising_connect):
            res = await cache.get("key")
            assert res is None
        await cache.close()

    with tempfile.NamedTemporaryFile(suffix=".db") as tf2:
        dq = SqliteDeque(tf2.name)
        with patch("aiosqlite.connect", side_effect=raising_connect):
            with pytest.raises(OSError):
                await dq.append(b"data")
        await dq.close()
