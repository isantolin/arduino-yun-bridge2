"""Integration-style tests for daemon async tasks."""

from __future__ import annotations

import asyncio
from collections import deque
from dataclasses import dataclass
from typing import Any, Deque, cast
from collections.abc import Awaitable, Callable, Coroutine
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from aiomqtt.message import Message

from cobs import cobs

from mcubridge.config.settings import RuntimeConfig
from mcubridge.rpc.protocol import FRAME_DELIMITER
from mcubridge.rpc.frame import Frame
from mcubridge.transport.mqtt import mqtt_task
from mcubridge.transport import (
    MAX_SERIAL_PACKET_BYTES,
    SerialTransport,
)
from mcubridge.rpc import protocol
from mcubridge.rpc.protocol import Command
from mcubridge.state.context import RuntimeState, create_runtime_state
from mcubridge.services.runtime import SerialHandshakeFatal
from mcubridge.transport.serial_fast import BridgeSerialProtocol


@dataclass
class _SerialServiceStub:
    config: RuntimeConfig
    state: RuntimeState

    def __post_init__(self) -> None:
        self.received_frames: Deque[tuple[int, bytes]] = deque()
        self.serial_connected = asyncio.Event()
        self.serial_disconnected = asyncio.Event()
        self._serial_sender: Callable[[int, bytes], Awaitable[bool]] | None = None

    def register_serial_sender(self, sender: Callable[[int, bytes], Awaitable[bool]]) -> None:
        self._serial_sender = sender

    async def on_serial_connected(self) -> None:
        self.serial_connected.set()

    async def on_serial_disconnected(self) -> None:
        self.serial_disconnected.set()

    async def handle_mcu_frame(self, command_id: int, payload: bytes) -> None:
        self.received_frames.append((command_id, payload))

    async def send_frame(self, command_id: int, payload: bytes = b"") -> bool:
        if self._serial_sender is None:
            return False
        return await self._serial_sender(command_id, payload)

    async def enqueue_mqtt(self, *_: object, **__: object) -> None:
        return None

    def is_command_allowed(self, _command: str) -> bool:
        return False

    async def schedule_background(
        self,
        coroutine: Coroutine[Any, Any, None],
        *,
        name: str | None = None,
    ) -> asyncio.Task[Any]:
        return asyncio.create_task(coroutine, name=name)


class _MQTTServiceStub:
    def __init__(self, state: RuntimeState) -> None:
        self.state = state
        self.handled = asyncio.Event()

    async def handle_mqtt_message(self, inbound: Message) -> None:
        self.handled.set()

    async def schedule_background(
        self,
        coroutine: Coroutine[Any, Any, None],
        *,
        name: str | None = None,
    ) -> asyncio.Task[Any]:
        return asyncio.create_task(coroutine, name=name)


class _FatalSerialServiceStub(_SerialServiceStub):
    async def on_serial_connected(self) -> None:
        raise SerialHandshakeFatal("fatal-handshake")


@pytest.mark.asyncio
async def test_serial_reader_task_processes_frame(
    monkeypatch: pytest.MonkeyPatch, runtime_config: RuntimeConfig
) -> None:
    state = create_runtime_state(runtime_config)
    service = _SerialServiceStub(runtime_config, state)

    payload = bytes([protocol.DIGITAL_HIGH])
    frame = Frame(Command.CMD_DIGITAL_READ_RESP.value, payload).to_bytes()
    encoded = cobs.encode(frame) + FRAME_DELIMITER

    # Mock Transport/Protocol
    mock_transport = MagicMock()
    mock_transport.is_closing.return_value = False

    # We need a real protocol to test data processing logic, or at least one with real methods
    # But BridgeSerialProtocol depends on asyncio loop.
    mock_protocol = BridgeSerialProtocol(service, state, asyncio.get_running_loop())
    mock_protocol.connection_made(mock_transport)

    async def _fake_create(*_: object, **__: object):
        return mock_transport, mock_protocol

    transport = SerialTransport(runtime_config, state, cast(Any, service))

    with patch("mcubridge.transport.serial_fast.serial_asyncio_fast.create_serial_connection", _fake_create):
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
    service = _SerialServiceStub(runtime_config, state)

    frame = Frame(
        Command.CMD_DIGITAL_READ_RESP.value,
        bytes([protocol.DIGITAL_HIGH]),
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

    with patch("mcubridge.transport.serial_fast.serial_asyncio_fast.create_serial_connection", _fake_create):
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
    service = _SerialServiceStub(runtime_config, state)

    TEST_PAYLOAD_BYTE = 0xAA
    oversized = bytes([TEST_PAYLOAD_BYTE]) * (MAX_SERIAL_PACKET_BYTES + 16)

    mock_transport = MagicMock()
    mock_transport.is_closing.return_value = False
    mock_protocol = BridgeSerialProtocol(service, state, asyncio.get_running_loop())
    mock_protocol.connection_made(mock_transport)

    async def _fake_create(*_: object, **__: object):
        return mock_transport, mock_protocol

    transport = SerialTransport(runtime_config, state, cast(Any, service))

    with patch("mcubridge.transport.serial_fast.serial_asyncio_fast.create_serial_connection", _fake_create):
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
    service = _FatalSerialServiceStub(runtime_config, state)

    mock_transport = MagicMock()
    mock_transport.is_closing.return_value = False
    mock_protocol = MagicMock()
    mock_protocol._connected_future = asyncio.get_running_loop().create_future()
    mock_protocol._connected_future.set_result(None)

    async def _fake_create(*_: object, **__: object):
        return mock_transport, mock_protocol

    transport = SerialTransport(runtime_config, state, cast(Any, service))

    with patch("mcubridge.transport.serial_fast.serial_asyncio_fast.create_serial_connection", _fake_create):
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
    service = _MQTTServiceStub(state)

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
