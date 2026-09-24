"""Tests for runtime service lifecycle and basic transport integration."""

from __future__ import annotations

from mcubridge.transport.serial import SerialTransport

import time
from unittest.mock import AsyncMock, MagicMock

import pytest
from pytest_mock import MockerFixture
from mcubridge.config.settings import RuntimeConfig
from mcubridge.protocol import protocol
from mcubridge.protocol import mcubridge_pb2 as pb
from mcubridge.services.runtime import BridgeService
from mcubridge.state.context import create_runtime_state


def _make_config() -> RuntimeConfig:
    fs_root = f".tmp_tests/fs-{time.time_ns()}"
    spool_dir = f".tmp_tests/spool-{time.time_ns()}"
    return RuntimeConfig(
        serial_port="/dev/test0",
        serial_baud=protocol.DEFAULT_BAUDRATE,
        serial_safe_baud=protocol.DEFAULT_SAFE_BAUDRATE,
        allowed_commands=("echo", "ls"),
        serial_shared_secret=b"testshared",
        file_system_root=fs_root,
        cloud_spool_dir=spool_dir,
        allow_non_tmp_paths=True,
    )


@pytest.mark.asyncio
async def test_send_frame_via_transport() -> None:
    service = None
    config = _make_config()
    state = create_runtime_state(config)
    try:
        mock_serial = AsyncMock(spec=SerialTransport)
        mock_serial.send.return_value = True
        service = BridgeService(config, state, mock_serial)

        assert service.serial is not None
        ok = await service.serial.send(protocol.Command.CMD_GET_VERSION.value, b"x")
        assert ok
        mock_serial.send.assert_called_once()
    finally:
        if service is not None:
            service.cleanup()
        else:
            state.cleanup()


@pytest.mark.asyncio
async def test_handle_mcu_frame_pre_sync_denied() -> None:
    service = None
    config = _make_config()
    state = create_runtime_state(config)
    try:
        mock_serial = AsyncMock(spec=SerialTransport)
        mock_serial.acknowledge.return_value = True
        service = BridgeService(config, state, mock_serial)

        # Before sync, MCU frames other than handshake are ignored/denied
        await service.handle_mcu_frame(protocol.Command.CMD_GET_VERSION.value, 1, b"")
        mock_serial.acknowledge.assert_not_called()
    finally:
        if service is not None:
            service.cleanup()
        else:
            state.cleanup()


@pytest.mark.asyncio
async def test_handle_mcu_xon_xoff() -> None:
    service = None
    config = _make_config()
    state = create_runtime_state(config)
    try:
        mock_serial = AsyncMock(spec=SerialTransport)
        service = BridgeService(config, state, mock_serial)
        state.state = "synchronized"

        await service.handle_mcu_frame(protocol.Command.CMD_XOFF.value, 1, b"")
        assert state.mcu_is_paused
        assert not state.serial_tx_allowed.is_set()

        await service.handle_mcu_frame(protocol.Command.CMD_XON.value, 2, b"")
        assert not state.mcu_is_paused
        assert state.serial_tx_allowed.is_set()
    finally:
        if service is not None:
            service.cleanup()
        else:
            state.cleanup()


@pytest.mark.asyncio
async def test_handle_cloud_console_queues_and_flushes() -> None:
    service = None
    config = _make_config()
    state = create_runtime_state(config)
    try:
        mock_serial = AsyncMock(spec=SerialTransport)
        mock_serial.send.return_value = True
        service = BridgeService(config, state, mock_serial)
        state.state = "synchronized"
        state.link_sync_event.set()
        state.serial_tx_allowed.set()

        class PublishPacket:
            def __init__(self, topic: str, payload: bytes) -> None:
                self.topic = topic
                self.payload = payload

        mock_msg = PublishPacket("br/console/in", b"hello")

        await service.handle_request(mock_msg)

        mock_serial.send.assert_called()
    finally:
        if service is not None:
            service.cleanup()
        else:
            state.cleanup()


@pytest.mark.asyncio
async def test_enqueue_cloud_spools_until_client_recovers() -> None:
    service = None
    config = _make_config()
    state = create_runtime_state(config)
    try:
        service = BridgeService(config, state, AsyncMock(spec=SerialTransport))
        message = pb.CloudQueuedPublish(topic_name="br/system/status", payload=b"payload")

        await service.enqueue_cloud(message)

        assert state.cloud_spool_pending_messages == 1

        mock_stream = MagicMock()
        mock_stream.send_message = AsyncMock()
        object.__setattr__(service, "_cloud_stream", mock_stream)
        await service.flush_cloud_spool()

        mock_stream.send_message.assert_called_once()
        assert state.cloud_spool_pending_messages == 0
    finally:
        if service is not None:
            service.cleanup()
        else:
            state.cleanup()


@pytest.mark.asyncio
async def test_handle_cloud_pin_overflow_reports_error(mocker: MockerFixture) -> None:
    service = None
    config = _make_config()
    state = create_runtime_state(config)
    try:
        from mcubridge.protocol.structures import PendingPinRequest
        mock_serial = AsyncMock(spec=SerialTransport)
        service = BridgeService(config, state, mock_serial)
        state.state = "synchronized"
        state.link_sync_event.set()
        state.pending_pin_request_limit = 1
        state.pending_digital_reads.append(PendingPinRequest(pin=13, reply_context=None))

        captured: list[pb.CloudQueuedPublish] = []

        async def capture_enqueue(message: pb.CloudQueuedPublish, *, reply_context: object | None = None) -> None:
            del reply_context
            captured.append(message)

        mocker.patch.object(service, "enqueue_cloud", side_effect=capture_enqueue)

        class PublishPacket:
            def __init__(self, topic: str, payload: bytes) -> None:
                self.topic = topic
                self.payload = payload
                self.properties = None

        message = PublishPacket("br/d/13/read", b"")

        await service.handle_request(message)

        assert captured
        assert any(
            isinstance(prop, pb.UserProperty) and prop.key == "bridge-error" and prop.value == "pending-pin-overflow"
            for prop in captured[0].user_properties
        )
        mock_serial.send.assert_not_called()
    finally:
        if service is not None:
            service.cleanup()
        else:
            state.cleanup()
