"""Integration-style tests for daemon async tasks."""

from __future__ import annotations

import asyncio
import contextlib
import struct
from collections import deque
from dataclasses import dataclass
from types import MethodType
from typing import Any, Deque, cast
from collections.abc import Awaitable, Callable, Coroutine
from unittest.mock import AsyncMock, MagicMock

import pytest
from aiomqtt.message import Message as MQTTMessage

from cobs import cobs

from yunbridge.config.settings import RuntimeConfig
from yunbridge.rpc.protocol import FRAME_DELIMITER
from yunbridge.rpc.frame import Frame
from yunbridge.transport.mqtt import mqtt_task
from yunbridge.transport.serial import (
    MAX_SERIAL_PACKET_BYTES,
    SerialTransport,
)
from yunbridge.rpc import protocol
from yunbridge.rpc.protocol import Command, Status
from yunbridge.state.context import RuntimeState, create_runtime_state
from yunbridge.services.runtime import SerialHandshakeFatal


class _FakeStreamWriter:
    def __init__(self) -> None:
        self.buffer: bytearray = bytearray()
        self._closing = False

    def write(self, data: bytes) -> int:
        self.buffer.extend(data)
        return len(data)

    async def drain(self) -> None:
        await asyncio.sleep(0)

    def is_closing(self) -> bool:
        return self._closing

    def close(self) -> None:
        self._closing = True

    async def wait_closed(self) -> None:
        await asyncio.sleep(0)


class _FakeStreamReader:
    def __init__(self, *chunks: bytes) -> None:
        self._bytes: Deque[int] = deque()
        for chunk in chunks:
            self._bytes.extend(chunk)

    async def read(self, _: int) -> bytes:
        await asyncio.sleep(0)
        if not self._bytes:
            return b""
        return bytes([self._bytes.popleft()])


@dataclass
class _SerialServiceStub:
    config: RuntimeConfig
    state: RuntimeState

    def __post_init__(self) -> None:
        self.received_frames: Deque[tuple[int, bytes]] = deque()
        self.serial_connected = asyncio.Event()
        self.serial_disconnected = asyncio.Event()
        self._serial_sender: Callable[[int, bytes], Awaitable[bool]] | None = (
            None
        )

    def register_serial_sender(
        self, sender: Callable[[int, bytes], Awaitable[bool]]
    ) -> None:
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

    async def handle_mqtt_message(self, inbound: MQTTMessage) -> None:
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

    reader = _FakeStreamReader(encoded, b"")
    writer = _FakeStreamWriter()

    async def _fake_open(*_: object, **__: object):
        return reader, writer

    monkeypatch.setattr(
        "yunbridge.transport.serial._open_serial_connection_with_retry",
        _fake_open,
    )

    transport = SerialTransport(runtime_config, state, cast(Any, service))
    task = asyncio.create_task(transport.run())

    await asyncio.wait_for(service.serial_connected.wait(), timeout=1)
    await asyncio.wait_for(service.serial_disconnected.wait(), timeout=1)

    assert service.received_frames
    command_id, received_payload = service.received_frames[0]
    assert command_id == Command.CMD_DIGITAL_READ_RESP.value
    assert received_payload == payload

    task.cancel()
    try:
        await task
    except asyncio.CancelledError:
        pass
    except Exception as exc:
        pytest.fail(f"Expected CancelledError, got {type(exc)}")


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

    reader = _FakeStreamReader(encoded, b"")
    writer = _FakeStreamWriter()

    async def _fake_open(*_: object, **__: object):
        return reader, writer

    monkeypatch.setattr(
        "yunbridge.transport.serial._open_serial_connection_with_retry",
        _fake_open,
    )

    transport = SerialTransport(runtime_config, state, cast(Any, service))
    task = asyncio.create_task(transport.run())

    await asyncio.wait_for(service.serial_connected.wait(), timeout=1)
    await asyncio.wait_for(service.serial_disconnected.wait(), timeout=1)

    assert not service.received_frames

    # Verify response in writer buffer
    assert writer.buffer
    packets = writer.buffer.split(FRAME_DELIMITER)
    # Remove empty trailing packet if buffer ended with terminator
    if not packets[-1]:
        packets.pop()

    assert packets
    decoded = cobs.decode(bytes(packets[0]))
    response_frame = Frame.from_bytes(decoded)

    assert response_frame.command_id == Status.CRC_MISMATCH.value

    task.cancel()
    with contextlib.suppress(asyncio.CancelledError):
        await task


@pytest.mark.asyncio
async def test_serial_reader_task_limits_packet_size(
    monkeypatch: pytest.MonkeyPatch, runtime_config: RuntimeConfig
) -> None:
    state = create_runtime_state(runtime_config)
    state.link_is_synchronized = True
    service = _SerialServiceStub(runtime_config, state)

    reported: Deque[tuple[int, bytes]] = deque()

    async def _capture_send_frame(
        self: _SerialServiceStub,
        command_id: int,
        payload: bytes,
    ) -> bool:
        reported.append((command_id, payload))
        return True

    service.send_frame = MethodType(_capture_send_frame, service)

    TEST_PAYLOAD_BYTE = 0xAA
    oversized = bytes([TEST_PAYLOAD_BYTE]) * (
        MAX_SERIAL_PACKET_BYTES + 16
    )
    # reader will produce oversized content then a terminator
    reader = _FakeStreamReader(oversized + FRAME_DELIMITER, b"")
    writer = _FakeStreamWriter()

    async def _fake_open(*_: object, **__: object):
        return reader, writer

    monkeypatch.setattr(
        "yunbridge.transport.serial._open_serial_connection_with_retry",
        _fake_open,
    )

    transport = SerialTransport(runtime_config, state, cast(Any, service))
    task = asyncio.create_task(transport.run())

    await asyncio.wait_for(service.serial_connected.wait(), timeout=1)
    await asyncio.wait_for(service.serial_disconnected.wait(), timeout=1)

    assert not service.received_frames
    assert reported
    status_id, payload = reported.pop()
    assert status_id == Status.MALFORMED.value
    assert payload[:2] == struct.pack(
        protocol.UINT16_FORMAT, protocol.INVALID_ID_SENTINEL
    )

    task.cancel()
    with contextlib.suppress(asyncio.CancelledError):
        await task


@pytest.mark.asyncio
async def test_serial_reader_task_propagates_handshake_fatal(
    monkeypatch: pytest.MonkeyPatch, runtime_config: RuntimeConfig
) -> None:
    state = create_runtime_state(runtime_config)
    service = _FatalSerialServiceStub(runtime_config, state)

    reader = _FakeStreamReader(b"")
    writer = _FakeStreamWriter()

    async def _fake_open(*_: object, **__: object):
        return reader, writer

    monkeypatch.setattr(
        "yunbridge.transport.serial._open_serial_connection_with_retry",
        _fake_open,
    )

    transport = SerialTransport(runtime_config, state, cast(Any, service))
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
        "yunbridge.transport.mqtt.aiomqtt.Client",
        lambda **_kw: mock_client,
    )

    runtime_config.mqtt_tls = False

    task = asyncio.create_task(
        mqtt_task(runtime_config, state, cast(Any, service))
    )

    await asyncio.wait_for(service.handled.wait(), timeout=1)

    task.cancel()
    try:
        await task
    except* asyncio.CancelledError:
        pass
