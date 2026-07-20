"""Sixth targeted coverage gap closure for runtime.py datastore, mailbox,
and filesystem handlers to achieve >95% overall Python coverage. [SIL-2]"""

from __future__ import annotations

from pathlib import Path
from typing import Iterator
from unittest.mock import AsyncMock, patch

import pytest

from mcubridge.config.settings import RuntimeConfig
from mcubridge.protocol.topics import parse_topic
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
# runtime.py — datastore, mailbox, and file handlers (lines 816-950)
# ==============================================================================


@pytest.mark.asyncio
async def test_handle_datastore_put_get_miss(cfg: RuntimeConfig, state: RuntimeState) -> None:
    """_handle_datastore covers GET cache miss & PUT quota check (lines 816-836)."""
    service = BridgeService(cfg, state, AsyncMock(spec=SerialTransport))

    fn_ds = getattr(service, "_handle_datastore")
    # 1. GET miss with "request" suffix -> publishes datastore-miss error
    route = parse_topic("br", "br/datastore/get/missing_key/request")
    assert route is not None
    with patch.object(service, "enqueue_cloud", new=AsyncMock()) as mock_enqueue:
        await fn_ds(route, BridgeRequest(topic="br/datastore/get/missing_key/request", payload=b""))
        mock_enqueue.assert_awaited_once()

    # 2. PUT key or payload > 255 bytes -> skipped
    route_put = parse_topic("br", "br/datastore/put/long_key")
    assert route_put is not None
    long_payload = b"x" * 300
    with patch.object(service, "enqueue_cloud", new=AsyncMock()) as mock_enqueue:
        await fn_ds(route_put, BridgeRequest(topic="br/datastore/put/long_key", payload=long_payload))
        mock_enqueue.assert_not_called()


@pytest.mark.asyncio
async def test_handle_mailbox_write_and_read(cfg: RuntimeConfig, state: RuntimeState) -> None:
    """_handle_mailbox covers WRITE and READ queue paths (lines 837-859)."""
    mock_serial = AsyncMock(spec=SerialTransport)
    service = BridgeService(cfg, state, mock_serial)

    fn_mb = getattr(service, "_handle_mailbox")
    # 1. WRITE -> pushes to mailbox_queue and sends CMD_MAILBOX_PUSH
    route_write = parse_topic("br", "br/mailbox/write")
    assert route_write is not None
    await fn_mb(route_write, BridgeRequest(topic="br/mailbox/write", payload=b"payload_data"))
    mock_serial.send.assert_awaited_once()

    # 2. READ from empty queue -> returns empty bytes
    route_read = parse_topic("br", "br/mailbox/read")
    assert route_read is not None
    with patch.object(service, "enqueue_cloud", new=AsyncMock()) as mock_enqueue:
        await fn_mb(route_read, BridgeRequest(topic="br/mailbox/read", payload=b""))
        mock_enqueue.assert_awaited_once()

    # 3. READ when queue has item -> pops item
    await state.mailbox_incoming_queue.append(b"queued_msg")
    with patch.object(service, "enqueue_cloud", new=AsyncMock()) as mock_enqueue:
        await fn_mb(route_read, BridgeRequest(topic="br/mailbox/read", payload=b""))
        mock_enqueue.assert_awaited_once()


@pytest.mark.asyncio
async def test_handle_file_mcu_operations(cfg: RuntimeConfig, state: RuntimeState) -> None:
    """_handle_file covers MCU target WRITE & REMOVE (lines 867-883)."""
    mock_serial = AsyncMock(spec=SerialTransport)
    mock_serial.send = AsyncMock(return_value=True)
    service = BridgeService(cfg, state, mock_serial)

    fn_file = getattr(service, "_handle_file")
    # 1. MCU WRITE
    route_write = parse_topic("br", "br/file/write/mcu/test.txt")
    assert route_write is not None
    with patch.object(service, "enqueue_cloud", new=AsyncMock()) as mock_enqueue:
        await fn_file(route_write, BridgeRequest(topic="br/file/write/mcu/test.txt", payload=b"content"))
        mock_serial.send.assert_awaited_once()
        mock_enqueue.assert_awaited_once()

    # 2. MCU REMOVE
    route_remove = parse_topic("br", "br/file/remove/mcu/test.txt")
    assert route_remove is not None
    await fn_file(route_remove, BridgeRequest(topic="br/file/remove/mcu/test.txt", payload=b""))
    assert mock_serial.send.call_count == 2


@pytest.mark.asyncio
async def test_handle_file_local_operations(tmp_path: Path, cfg: RuntimeConfig, state: RuntimeState) -> None:
    """_handle_file covers local WRITE, READ, and REMOVE (lines 884-913)."""
    mock_serial = AsyncMock(spec=SerialTransport)
    service = BridgeService(cfg, state, mock_serial)

    fn_file = getattr(service, "_handle_file")
    local_file = tmp_path / "fs" / "local_test.txt"
    local_file.parent.mkdir(parents=True, exist_ok=True)

    # 1. Local WRITE
    route_write = parse_topic("br", "br/file/write/local_test.txt")
    assert route_write is not None
    with patch.object(service, "enqueue_cloud", new=AsyncMock()) as mock_enqueue:
        await fn_file(route_write, BridgeRequest(topic="br/file/write/local_test.txt", payload=b"hello local"))
        assert local_file.exists()
        assert local_file.read_bytes() == b"hello local"
        mock_enqueue.assert_awaited_once()

    # 2. Local READ
    route_read = parse_topic("br", "br/file/read/local_test.txt")
    assert route_read is not None
    with patch.object(service, "enqueue_cloud", new=AsyncMock()) as mock_enqueue:
        await fn_file(route_read, BridgeRequest(topic="br/file/read/local_test.txt", payload=b""))
        mock_enqueue.assert_awaited_once()

    # 3. Local REMOVE
    route_remove = parse_topic("br", "br/file/remove/local_test.txt")
    assert route_remove is not None
    await fn_file(route_remove, BridgeRequest(topic="br/file/remove/local_test.txt", payload=b""))
    assert not local_file.exists()


@pytest.mark.asyncio
async def test_handle_file_mcu_read_dispatch_failed(cfg: RuntimeConfig, state: RuntimeState) -> None:
    """_handle_file_mcu_read handles send_raw failure (lines 928-942)."""
    mock_serial = AsyncMock(spec=SerialTransport)
    mock_serial.send_raw = AsyncMock(return_value=False)
    service = BridgeService(cfg, state, mock_serial)

    req = BridgeRequest(topic="br/file/read/mcu/sd/test.bin", payload=b"")
    with patch.object(service, "enqueue_cloud", new=AsyncMock()) as mock_enqueue:
        fn_read = getattr(service, "_handle_file_mcu_read")
        await fn_read(req, "mcu/sd/test.bin")
        mock_enqueue.assert_awaited_once()
        args = mock_enqueue.call_args[0][0]
        assert b"error:mcu_file_read_dispatch_failed" in args.payload
