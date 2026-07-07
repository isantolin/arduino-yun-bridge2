"""Focused unit tests for BridgeService (runtime)."""

from __future__ import annotations
from mcubridge.transport.serial import SerialTransport

import time
from unittest.mock import AsyncMock

import pytest
from mcubridge.config.settings import RuntimeConfig
from mcubridge.protocol import protocol
from mcubridge.protocol.structures import create_queued_publish
from mcubridge.protocol import mcubridge_pb2 as pb
from mcubridge.services.runtime import BridgeService
from mcubridge.state.context import create_runtime_state


def _make_config() -> RuntimeConfig:
    import os

    fs_root = f".tmp_tests/mcubridge-test-fs-{os.getpid()}-{time.time_ns()}"
    spool_dir = f".tmp_tests/mcubridge-test-spool-{os.getpid()}-{time.time_ns()}"
    return RuntimeConfig(
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
        assert ok is True
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
        service = BridgeService(config, state, mock_serial)
        state.state = "unsynchronized"

        # CMD_GET_VERSION is not in pre-sync allowed list (64 is MIN_SYS but not sync/reset)
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
        service = BridgeService(
            config,
            state,
            AsyncMock(spec=SerialTransport),
        )
        state.state = "synchronized"

        await service.handle_mcu_frame(protocol.Command.CMD_XOFF.value, 1, b"")
        assert state.mcu_is_paused is True
        assert state.serial_tx_allowed.is_set() is False

        await service.handle_mcu_frame(protocol.Command.CMD_XON.value, 2, b"")
        assert state.mcu_is_paused is False
        assert state.serial_tx_allowed.is_set() is True
    finally:
        if service is not None:
            service.cleanup()
        else:
            state.cleanup()


@pytest.mark.asyncio
async def test_handle_mqtt_console_queues_and_flushes() -> None:
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
async def test_enqueue_mqtt_spools_until_client_recovers() -> None:
    service = None
    config = _make_config()
    state = create_runtime_state(config)
    try:
        service = BridgeService(config, state, AsyncMock(spec=SerialTransport))
        message = create_queued_publish("br/system/status", b"payload")

        await service.enqueue_mqtt(message)

        assert state.mqtt_spool_pending_messages == 1

        mock_client = AsyncMock()
        service._cloud_writer = mock_client
        await service.flush_mqtt_spool()

        mock_client.write.assert_called_once()
        assert state.mqtt_spool_pending_messages == 0
    finally:
        if service is not None:
            service.cleanup()
        else:
            state.cleanup()


@pytest.mark.asyncio
async def test_handle_mqtt_pin_overflow_reports_error() -> None:
    service = None
    config = _make_config()
    state = create_runtime_state(config)
    try:
        from unittest.mock import patch
        from mcubridge.protocol.structures import PendingPinRequest

        mock_serial = AsyncMock(spec=SerialTransport)
        service = BridgeService(config, state, mock_serial)
        state.state = "synchronized"
        state.link_sync_event.set()
        state.pending_pin_request_limit = 1
        state.pending_digital_reads.append(PendingPinRequest(pin=13, reply_context=None))

        captured: list[pb.MqttQueuedPublish] = []

        async def capture_enqueue(message: pb.MqttQueuedPublish, *, reply_context: object | None = None) -> None:
            del reply_context
            captured.append(message)

        with patch.object(service, "enqueue_mqtt", side_effect=capture_enqueue):

            class PublishPacket:
                def __init__(self, topic: str, payload: bytes) -> None:
                    self.topic = topic
                    self.payload = payload
                    self.properties = None

            message = PublishPacket("br/d/13/read", b"")

            await service.handle_request(message)

        assert captured
        assert any(
            prop.key == "bridge-error" and prop.value == "pending-pin-overflow" for prop in captured[0].user_properties
        )
        mock_serial.send.assert_not_called()
    finally:
        if service is not None:
            service.cleanup()
        else:
            state.cleanup()
