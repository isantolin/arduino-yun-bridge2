"""Targeted regression tests for uncovered runtime control-flow branches."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest
from pytest_mock import MockerFixture

from mcubridge.protocol import mcubridge_pb2 as pb
from mcubridge.protocol import protocol
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
    assert len(mock_bridge_service.state.console_to_mcu_queue) == 0


async def test_console_write_from_mcu_empty_payload_is_noop(mock_bridge_service: BridgeService) -> None:
    response = await mock_bridge_service.console_write(0, pb.ConsoleWrite(data=b""))

    assert response.status == "ok"


async def test_reset_link_without_serial_returns_false(mock_bridge_service: BridgeService) -> None:
    mock_bridge_service.serial = None

    assert await mock_bridge_service.reset_link() is False


async def test_send_cloud_event_without_cloud_stream_is_noop(
    mock_bridge_service: BridgeService,
    mocker: MockerFixture,
) -> None:
    mock_bridge_service.cloud_stream = None
    debug_log = mocker.patch("mcubridge.services.runtime.logger.debug")
    mocker.patch("mcubridge.services.runtime.logger.is_enabled_for", return_value=True)

    await mock_bridge_service.send_cloud_event("status_online", "info", "Device online")

    debug_log.assert_not_called()


async def test_send_cloud_event_debug_logging_branch(
    mock_bridge_service: BridgeService,
    mocker: MockerFixture,
) -> None:
    stream = MagicMock()
    stream.send_message = AsyncMock()
    mock_bridge_service.cloud_stream = stream
    debug_log = mocker.patch("mcubridge.services.runtime.logger.debug")
    mocker.patch("mcubridge.services.runtime.logger.is_enabled_for", return_value=True)

    await mock_bridge_service.send_cloud_event("status_online", "info", "Device online")

    debug_log.assert_called_once()
    stream.send_message.assert_awaited_once()


async def test_handle_mcu_frame_without_serial_is_noop(
    mock_bridge_service: BridgeService,
) -> None:
    mock_bridge_service.serial = None

    await mock_bridge_service.handle_mcu_frame(protocol.Command.CMD_XOFF.value, 1, b"")

    assert not mock_bridge_service.state.mcu_is_paused


async def test_handle_mcu_frame_tuple_falls_back_to_command_id_alias(
    mock_bridge_service: BridgeService,
) -> None:
    service = mock_bridge_service
    service.state.state = "synchronized"
    handler = AsyncMock()
    service.mcu_registry.clear()
    service.mcu_registry[protocol.Command.CMD_XOFF.value] = handler

    await service.handle_mcu_frame((0x1234, protocol.Command.CMD_XOFF.value), 9, b"")

    handler.assert_awaited_once_with(9, b"")


async def test_on_serial_disconnected_without_serial_transport(
    mock_bridge_service: BridgeService,
) -> None:
    mock_bridge_service.serial = None

    await mock_bridge_service.on_serial_disconnected()

    assert not mock_bridge_service.state.mcu_is_paused


async def test_flush_console_queue_without_serial_is_noop(
    mock_bridge_service: BridgeService,
) -> None:
    mock_bridge_service.serial = None
    mock_bridge_service.state.console_to_mcu_queue.append(b"pending")

    await mock_bridge_service.flush_console_queue()

    assert len(mock_bridge_service.state.console_to_mcu_queue) == 1
