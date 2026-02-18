"""Integration-style tests for daemon async tasks."""

from __future__ import annotations

import asyncio
from typing import Any, cast
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from cobs import cobs
from mcubridge.config.settings import RuntimeConfig
from mcubridge.protocol import protocol
from mcubridge.protocol.frame import Frame
from mcubridge.protocol.protocol import FRAME_DELIMITER, Command
from mcubridge.services.handshake import SerialHandshakeFatal
from mcubridge.state.context import create_runtime_state
from mcubridge.transport import (
    MAX_SERIAL_PACKET_BYTES,
    SerialTransport,
)
from mcubridge.transport.mqtt import mqtt_task
from mcubridge.transport.serial import BridgeSerialProtocol

# [REDUCTION] Use shared mocks to avoid duplication
from tests.mocks import MockFatalSerialService, MockMQTTService, MockSerialService


@pytest.mark.asyncio
async def test_serial_reader_task_processes_frame(
    monkeypatch: pytest.MonkeyPatch, runtime_config: RuntimeConfig
) -> None:
    state = create_runtime_state(runtime_config)
    service = MockSerialService(runtime_config, state)

    payload = bytes([protocol.DIGITAL_HIGH])
    frame = Frame(command_id=Command.CMD_DIGITAL_READ_RESP.value, payload=payload).to_bytes()
    encoded = cobs.encode(frame) + FRAME_DELIMITER

    # Mock Transport/Protocol
    mock_transport = MagicMock()
    mock_transport.is_closing.return_value = False

    # We need a real protocol to test data processing logic, or at least one with real methods
    mock_protocol = BridgeSerialProtocol(service, state, asyncio.get_running_loop())
    mock_protocol.connection_made(mock_transport)

    async def _fake_create(*_: object, **__: object):
        return mock_transport, mock_protocol

    transport = SerialTransport(runtime_config, state, cast(Any, service))

    with patch("mcubridge.transport.serial.serial_asyncio_fast.create_serial_connection", _fake_create), \
         patch.object(SerialTransport, "_toggle_dtr", new_callable=AsyncMock):
        task = asyncio.create_task(transport.run())

        await asyncio.wait_for(service.serial_connected.wait(), timeout=1)

        # Inject data
        mock_protocol.data_received(encoded)

        # Allow async processing
        await asyncio.sleep(0.01)

        assert service.received_frames
        command_id, received_payload = service.received_frames[0]
        assert command_id == Command.CMD_DIGITAL_READ_RESP.value
        assert received_payload == payload

        mock_transport.is_closing.return_value = True
        # Break the run loop
        with patch("asyncio.sleep", side_effect=RuntimeError("Stop")):
            try:
                await task
            except RuntimeError:
                pass
            except asyncio.CancelledError:
                pass


@pytest.mark.asyncio
async def test_serial_reader_task_emits_crc_mismatch(
    monkeypatch: pytest.MonkeyPatch, runtime_config: RuntimeConfig
) -> None:
    state = create_runtime_state(runtime_config)
    state.link_is_synchronized = True
    service = MockSerialService(runtime_config, state)

    frame = Frame(
        command_id=Command.CMD_DIGITAL_READ_RESP.value,
        payload=bytes([protocol.DIGITAL_HIGH]),
    ).to_bytes()
    corrupted = bytearray(cobs.encode(frame))
    corrupted[0] = protocol.UINT8_MASK  # Invalid COBS code
    encoded = cobs.encode(bytes(corrupted)) + FRAME_DELIMITER

    mock_transport = MagicMock()
    mock_transport.is_closing.return_value = False
    mock_protocol = BridgeSerialProtocol(service, state, asyncio.get_running_loop())
    mock_protocol.connection_made(mock_transport)

    async def _fake_create(*_: object, **__: object):
        return mock_transport, mock_protocol

    transport = SerialTransport(runtime_config, state, cast(Any, service))

    with patch("mcubridge.transport.serial.serial_asyncio_fast.create_serial_connection", _fake_create), \
         patch.object(SerialTransport, "_toggle_dtr", new_callable=AsyncMock):
        task = asyncio.create_task(transport.run())
        await asyncio.wait_for(service.serial_connected.wait(), timeout=1)

        # Inject data
        mock_protocol.data_received(encoded)
        await asyncio.sleep(0.01)

        assert not service.received_frames
        # Should record error
        assert state.serial_decode_errors > 0

        # Stop
        mock_transport.is_closing.return_value = True
        with patch("asyncio.sleep", side_effect=RuntimeError("Stop")):
            try:
                await task
            except RuntimeError:
                pass


@pytest.mark.asyncio
async def test_serial_reader_task_limits_packet_size(
    monkeypatch: pytest.MonkeyPatch, runtime_config: RuntimeConfig
) -> None:
    state = create_runtime_state(runtime_config)
    state.link_is_synchronized = True
    service = MockSerialService(runtime_config, state)

    TEST_PAYLOAD_BYTE = 0xAA
    oversized = bytes([TEST_PAYLOAD_BYTE]) * (MAX_SERIAL_PACKET_BYTES + 16)

    mock_transport = MagicMock()
    mock_transport.is_closing.return_value = False
    mock_protocol = BridgeSerialProtocol(service, state, asyncio.get_running_loop())
    mock_protocol.connection_made(mock_transport)

    async def _fake_create(*_: object, **__: object):
        return mock_transport, mock_protocol

    transport = SerialTransport(runtime_config, state, cast(Any, service))

    with patch("mcubridge.transport.serial.serial_asyncio_fast.create_serial_connection", _fake_create), \
         patch.object(SerialTransport, "_toggle_dtr", new_callable=AsyncMock):
        task = asyncio.create_task(transport.run())
        await asyncio.wait_for(service.serial_connected.wait(), timeout=1)

        mock_protocol.data_received(oversized + FRAME_DELIMITER)
        await asyncio.sleep(0.01)

        assert not service.received_frames
        assert state.serial_decode_errors >= 1

        mock_transport.is_closing.return_value = True
        with patch("asyncio.sleep", side_effect=RuntimeError("Stop")):
            try:
                await task
            except RuntimeError:
                pass


@pytest.mark.asyncio
async def test_serial_reader_task_propagates_handshake_fatal(
    monkeypatch: pytest.MonkeyPatch, runtime_config: RuntimeConfig
) -> None:
    state = create_runtime_state(runtime_config)
    service = MockFatalSerialService(runtime_config, state)

    mock_transport = MagicMock()
    mock_transport.is_closing.return_value = False
    mock_protocol = MagicMock()
    mock_protocol.connected_future = asyncio.get_running_loop().create_future()
    mock_protocol.connected_future.set_result(None)

    async def _fake_create(*_: object, **__: object):
        return mock_transport, mock_protocol

    transport = SerialTransport(runtime_config, state, cast(Any, service))

    with patch("mcubridge.transport.serial.serial_asyncio_fast.create_serial_connection", _fake_create), \
         patch.object(SerialTransport, "_toggle_dtr", new_callable=AsyncMock):
        task = asyncio.create_task(transport.run())

        try:
            await task
        except SerialHandshakeFatal:
            pass
        except Exception as exc:
            pytest.fail(f"Expected SerialHandshakeFatal, got {type(exc)}")
        else:
            pytest.fail("Did not raise SerialHandshakeFatal")


@pytest.mark.asyncio
async def test_mqtt_task_handles_incoming_message(
    monkeypatch: pytest.MonkeyPatch, runtime_config: RuntimeConfig
) -> None:
    state = create_runtime_state(runtime_config)
    state.mqtt_topic_prefix = runtime_config.mqtt_topic
    service = MockMQTTService(state)

    # Mock aiomqtt Client
    mock_client = AsyncMock()
    mock_client.__aenter__.return_value = mock_client
    mock_client.__aexit__.return_value = None

    # Mock messages iterator
    mock_msgs_ctx = AsyncMock()
    mock_client.messages = mock_msgs_ctx

    # Mock iterator
    fake_msg = MagicMock()
    fake_msg.topic = f"{state.mqtt_topic_prefix}/console/in"
    fake_msg.payload = b"hi"
    fake_msg.qos = 0
    fake_msg.retain = False
    fake_msg.properties = None

    async def msg_gen():
        yield fake_msg

    mock_msgs_ctx.__aiter__.side_effect = msg_gen

    monkeypatch.setattr(
        "mcubridge.transport.mqtt.aiomqtt.Client",
        lambda **_kw: mock_client,
    )

    runtime_config.mqtt_tls = False

    task = asyncio.create_task(mqtt_task(runtime_config, state, cast(Any, service)))

    await asyncio.wait_for(service.handled.wait(), timeout=1)

    task.cancel()
    try:
        await task
    except* asyncio.CancelledError:
        pass
