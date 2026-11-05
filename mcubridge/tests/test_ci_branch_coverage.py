"""Targeted regression tests for uncovered runtime control-flow branches."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest
from pytest_mock import MockerFixture

from mcubridge.protocol import mcubridge_pb2 as pb
from mcubridge.services.runtime import BridgeService


async def test_publish_cloud_message_debug_logging_branch(
    mock_bridge_service: BridgeService,
    mocker: MockerFixture,
) -> None:
    service = mock_bridge_service
    stream = MagicMock()
    stream.send_message = AsyncMock()
    service.cloud_stream = stream
    mocker.patch("mcubridge.services.runtime.logger.is_enabled_for", return_value=True)

    message = pb.CloudQueuedPublish(topic_name="bridge/status", payload=b"online")
    assert await service.publish_cloud_message(message) is True
    stream.send_message.assert_awaited_once()


def test_cleanup_without_serial_transport(mock_bridge_service: BridgeService) -> None:
    mock_bridge_service.serial = None

    mock_bridge_service.cleanup()

    assert mock_bridge_service.serial is None


async def test_on_serial_connected_rejects_unsynchronized_link(
    mock_bridge_service: BridgeService,
    mocker: MockerFixture,
) -> None:
    mocker.patch.object(mock_bridge_service.handshake, "synchronize", new=AsyncMock(return_value=False))

    with pytest.raises(ConnectionError, match="handshake synchronization failed"):
        await mock_bridge_service.on_serial_connected()


async def test_file_read_unsafe_path_without_serial_returns_empty(
    mock_bridge_service: BridgeService,
) -> None:
    mock_bridge_service.serial = None

    response = await mock_bridge_service.file_read(1, pb.FileRead(path="../../../etc/passwd"))

    assert response.content == b""


async def test_console_write_empty_payload_is_noop(mock_bridge_service: BridgeService) -> None:
    response = await mock_bridge_service.console_write(pb.ConsoleWrite(data=b""))

    assert response.status == "ok"
    assert mock_bridge_service.state.console_to_mcu_queue == []


async def test_reset_link_without_serial_returns_false(mock_bridge_service: BridgeService) -> None:
    mock_bridge_service.serial = None

    assert await mock_bridge_service.reset_link() is False


async def test_send_cloud_event_without_cloud_stream_is_noop(mock_bridge_service: BridgeService) -> None:
    mock_bridge_service.cloud_stream = None

    await mock_bridge_service.send_cloud_event("status_online", "info", "Device online")
