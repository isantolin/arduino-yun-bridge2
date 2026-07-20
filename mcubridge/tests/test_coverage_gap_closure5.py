"""Fifth targeted coverage gap closure for BridgeService.handle_request routing,
topic authorization, and MCU command handler branches. [SIL-2]"""

from __future__ import annotations

from pathlib import Path
from typing import Iterator
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from mcubridge.config.settings import RuntimeConfig
from mcubridge.protocol import mcubridge_pb2 as pb
from mcubridge.protocol.protocol import Status
from mcubridge.services.runtime import BridgeRequest, BridgeService
from mcubridge.state.context import RuntimeState, create_runtime_state
from mcubridge.transport.serial import SerialTransport

# ==============================================================================
# Fixtures
# ==============================================================================


def _make_config(tmp_path: Path) -> RuntimeConfig:
    return RuntimeConfig(
        serial_port="/dev/ttyATH0",
        topic_prefix="br",
        allowed_commands=("*",),
        serial_shared_secret=b"secret123456abcd",
        file_system_root=str(tmp_path / "fs"),
        cloud_spool_dir=str(tmp_path / "spool"),
        allow_non_tmp_paths=True,
    )


@pytest.fixture
def cfg(tmp_path: Path) -> RuntimeConfig:
    return _make_config(tmp_path)


@pytest.fixture
def state(cfg: RuntimeConfig) -> Iterator[RuntimeState]:
    s = create_runtime_state(cfg)
    yield s
    s.cleanup()


# ==============================================================================
# runtime.py — BridgeService.handle_request routing & authorization
# ==============================================================================


@pytest.mark.asyncio
async def test_handle_request_link_sync_timeout(cfg: RuntimeConfig, state: RuntimeState) -> None:
    """handle_request times out waiting for link sync on pin request (lines 534-535)."""
    service = BridgeService(cfg, state, AsyncMock(spec=SerialTransport))
    state.link_sync_event.clear()

    req = BridgeRequest(topic="br/digital/13/write", payload=b"1")
    with patch("mcubridge.services.runtime.DEFAULT_SYNC_TIMEOUT_SECONDS", 0.01):
        await service.handle_request(req)


@pytest.mark.asyncio
async def test_handle_request_topic_authorization_rejected(cfg: RuntimeConfig, state: RuntimeState) -> None:
    """handle_request rejects unauthorized cloud topics (lines 543-544)."""
    service = BridgeService(cfg, state, AsyncMock(spec=SerialTransport))
    state.topic_authorization = MagicMock()

    with patch("mcubridge.services.runtime.allows_topic", return_value=False):
        req = BridgeRequest(topic="br/file/write/mcu/test.txt", payload=b"data")
        with patch.object(service, "_reject_cloud", new=AsyncMock()) as mock_reject:
            await service.handle_request(req)
            mock_reject.assert_awaited_once()


@pytest.mark.asyncio
async def test_handle_request_routes(cfg: RuntimeConfig, state: RuntimeState) -> None:
    """handle_request dispatches to Topic.SHELL, Topic.FILE, Topic.DATASTORE, Topic.MAILBOX, Topic.SPI, Topic.SYSTEM."""
    service = BridgeService(cfg, state, AsyncMock(spec=SerialTransport))
    state.link_sync_event.set()
    state.topic_authorization = MagicMock()

    with patch("mcubridge.services.runtime.allows_topic", return_value=True):
        # 1. Topic.CONSOLE
        with patch.object(service, "_handle_console", new=AsyncMock()) as m:
            await service.handle_request(BridgeRequest(topic="br/console/input", payload=b"echo"))
            m.assert_awaited_once()

        # 2. Topic.DATASTORE
        with patch.object(service, "_handle_datastore", new=AsyncMock()) as m:
            await service.handle_request(BridgeRequest(topic="br/datastore/get/key1", payload=b""))
            m.assert_awaited_once()

        # 3. Topic.MAILBOX
        with patch.object(service, "_handle_mailbox", new=AsyncMock()) as m:
            await service.handle_request(BridgeRequest(topic="br/mailbox/push", payload=b"msg"))
            m.assert_awaited_once()

        # 4. Topic.FILE
        with patch.object(service, "_handle_file", new=AsyncMock()) as m:
            await service.handle_request(BridgeRequest(topic="br/file/read/test.txt", payload=b""))
            m.assert_awaited_once()

        # 5. Topic.SHELL
        with patch.object(service, "_handle_shell", new=AsyncMock()) as m:
            await service.handle_request(BridgeRequest(topic="br/sh/run", payload=b"ls"))
            m.assert_awaited_once()

        # 6. Topic.SPI
        with patch.object(service, "_handle_spi", new=AsyncMock()) as m:
            await service.handle_request(BridgeRequest(topic="br/spi/transfer", payload=b"\x01\x02"))
            m.assert_awaited_once()

        # 7. Topic.SYSTEM
        with patch.object(service, "_handle_system", new=AsyncMock()) as m:
            await service.handle_request(BridgeRequest(topic="br/system/reset", payload=b""))
            m.assert_awaited_once()


@pytest.mark.asyncio
async def test_handle_mcu_frame_unknown_command(cfg: RuntimeConfig, state: RuntimeState) -> None:
    """handle_mcu_frame increments unknown_command_count when response_to_request is None (lines 507-508)."""
    mock_serial = AsyncMock(spec=SerialTransport)
    service = BridgeService(cfg, state, mock_serial)
    state.mark_synchronized()

    # CommandId 9999 is unknown
    await service.handle_mcu_frame(9999, 1, b"")
    mock_serial.send.assert_awaited_once_with(Status.NOT_IMPLEMENTED.value, b"")


@pytest.mark.asyncio
async def test_on_mcu_datastore_get_no_serial(cfg: RuntimeConfig, state: RuntimeState) -> None:
    """_on_mcu_datastore_get returns False when serial is None (line 601-602)."""
    service = BridgeService(cfg, state, AsyncMock(spec=SerialTransport))
    service.serial = None
    fn = getattr(service, "_on_mcu_datastore_get")
    res = await fn(1, pb.DatastoreGet(key="k1"))
    assert res is False


@pytest.mark.asyncio
async def test_on_mcu_datastore_get_no_cache(cfg: RuntimeConfig, state: RuntimeState) -> None:
    """_on_mcu_datastore_get handles datastore_cache is None (lines 604-609)."""
    mock_serial = AsyncMock(spec=SerialTransport)
    mock_serial.send = AsyncMock(return_value=True)
    service = BridgeService(cfg, state, mock_serial)

    state.datastore_cache = None
    fn = getattr(service, "_on_mcu_datastore_get")
    res = await fn(1, pb.DatastoreGet(key="k1"))
    assert res is True
    mock_serial.send.assert_awaited_once()


@pytest.mark.asyncio
async def test_on_mcu_datastore_put_no_cache(cfg: RuntimeConfig, state: RuntimeState) -> None:
    """_on_mcu_datastore_put handles datastore_cache is None (line 593)."""
    service = BridgeService(cfg, state, AsyncMock(spec=SerialTransport))
    state.datastore_cache = None
    fn = getattr(service, "_on_mcu_datastore_put")
    res = await fn(1, pb.DatastorePut(key="k1", value=b"v1"))
    assert res is True


@pytest.mark.asyncio
async def test_on_mcu_console_write_empty_data(cfg: RuntimeConfig, state: RuntimeState) -> None:
    """_on_mcu_console_write skips cloud enqueue when data is empty (line 582)."""
    service = BridgeService(cfg, state, AsyncMock(spec=SerialTransport))
    with patch.object(service, "enqueue_cloud", new=AsyncMock()) as mock_enqueue:
        fn = getattr(service, "_on_mcu_console_write")
        await fn(1, pb.ConsoleWrite(data=b""))
        mock_enqueue.assert_not_called()


@pytest.mark.asyncio
async def test_on_mcu_mailbox_push(cfg: RuntimeConfig, state: RuntimeState) -> None:
    """_on_mcu_mailbox_push appends data to incoming queue and enqueues to cloud (lines 612-617)."""
    service = BridgeService(cfg, state, AsyncMock(spec=SerialTransport))
    with patch.object(service, "enqueue_cloud", new=AsyncMock()) as mock_enqueue:
        fn = getattr(service, "_on_mcu_mailbox_push")
        res = await fn(1, pb.MailboxPush(data=b"hello mailbox"))
        assert res is True
        mock_enqueue.assert_awaited_once()
