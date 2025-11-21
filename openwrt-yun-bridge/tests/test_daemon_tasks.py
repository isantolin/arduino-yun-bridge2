"""Integration-style tests for daemon async tasks."""
from __future__ import annotations

import asyncio
import contextlib
from collections import deque
from dataclasses import dataclass
from types import TracebackType
from typing import Any, Awaitable, Callable, Coroutine, Deque, Optional, cast

import pytest

from yunbridge.common import cobs_encode
from yunbridge.config.settings import RuntimeConfig
from yunbridge.const import SERIAL_TERMINATOR
from yunbridge.daemon import Frame, mqtt_task, serial_reader_task
from yunbridge.mqtt import InboundMessage
from yunbridge.rpc.protocol import Command, Status
from yunbridge.state.context import RuntimeState, create_runtime_state


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
        self._serial_sender: Optional[
            Callable[[int, bytes], Awaitable[bool]]
        ] = None

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

    def schedule_background(
        self, coroutine: Coroutine[Any, Any, None]
    ) -> None:
        asyncio.create_task(coroutine)


class _FakeMQTTClient:
    def __init__(self, messages: Deque[object]) -> None:
        self.messages = messages
        self.subscriptions: list[tuple[str, int]] = []
        self.published: list[tuple[str, bytes, int, bool]] = []

    async def connect(self) -> None:
        return None

    async def disconnect(self) -> None:
        return None

    async def publish(
        self,
        topic: str,
        payload: bytes,
        *,
        qos: int,
        retain: bool,
    ) -> None:
        self.published.append((topic, payload, qos, retain))

    async def subscribe(self, topic: str, qos: int) -> None:
        self.subscriptions.append((topic, qos))

    def unfiltered_messages(self):
        client = self

        class _Stream:
            async def __aenter__(self) -> _Stream:
                return self

            async def __aexit__(
                self,
                exc_type: type[BaseException] | None,
                exc: BaseException | None,
                tb: TracebackType | None,
            ) -> bool:
                return False

            def __aiter__(self) -> _Stream:
                return self

            async def __anext__(self) -> object:
                await asyncio.sleep(0)
                if client.messages:
                    return client.messages.popleft()
                raise StopAsyncIteration

        return _Stream()


class _MQTTServiceStub:
    def __init__(self, state: RuntimeState) -> None:
        self.state = state
        self.handled = asyncio.Event()

    async def handle_mqtt_message(
        self, inbound: InboundMessage
    ) -> None:
        self.handled.set()

    def schedule_background(
        self, coroutine: Coroutine[Any, Any, None]
    ) -> None:
        asyncio.create_task(coroutine)


def test_serial_reader_task_processes_frame(
    monkeypatch: pytest.MonkeyPatch, runtime_config: RuntimeConfig
) -> None:
    async def _run() -> None:
        state = create_runtime_state(runtime_config)
        service = _SerialServiceStub(runtime_config, state)

        payload = b"\x01"
        frame = Frame(Command.CMD_DIGITAL_READ_RESP.value, payload).to_bytes()
        encoded = cobs_encode(frame) + SERIAL_TERMINATOR

        reader = _FakeStreamReader(encoded, b"")
        writer = _FakeStreamWriter()

        async def _fake_open(*_: object, **__: object):
            return reader, writer

        monkeypatch.setattr(
            "yunbridge.daemon._open_serial_connection_with_retry",
            _fake_open,
        )

        task = asyncio.create_task(
            serial_reader_task(runtime_config, state, cast(Any, service))
        )

        await asyncio.wait_for(service.serial_connected.wait(), timeout=1)
        await asyncio.wait_for(service.serial_disconnected.wait(), timeout=1)

        assert service.received_frames
        command_id, received_payload = service.received_frames[0]
        assert command_id == Command.CMD_DIGITAL_READ_RESP.value
        assert received_payload == payload

        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

    asyncio.run(_run())


def test_serial_reader_task_emits_crc_mismatch(
    monkeypatch: pytest.MonkeyPatch, runtime_config: RuntimeConfig
) -> None:
    async def _run() -> None:
        state = create_runtime_state(runtime_config)
        service = _SerialServiceStub(runtime_config, state)

        status_frames: Deque[tuple[int, bytes]] = deque()

        async def fake_sender(command_id: int, payload: bytes) -> bool:
            status_frames.append((command_id, payload))
            return True

        service.register_serial_sender(fake_sender)

        frame = Frame(Command.CMD_DIGITAL_READ_RESP.value, b"\x01").to_bytes()
        corrupted = bytearray(frame)
        corrupted[-1] ^= 0xFF
        encoded = cobs_encode(bytes(corrupted)) + SERIAL_TERMINATOR

        reader = _FakeStreamReader(encoded, b"")
        writer = _FakeStreamWriter()

        async def _fake_open(*_: object, **__: object):
            return reader, writer

        monkeypatch.setattr(
            "yunbridge.daemon._open_serial_connection_with_retry",
            _fake_open,
        )

        task = asyncio.create_task(
            serial_reader_task(runtime_config, state, cast(Any, service))
        )

        await asyncio.wait_for(service.serial_connected.wait(), timeout=1)
        await asyncio.wait_for(service.serial_disconnected.wait(), timeout=1)

        assert not service.received_frames
        assert status_frames
        assert any(
            command_id == Status.CRC_MISMATCH.value
            for command_id, _ in status_frames
        )

        task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await task

    asyncio.run(_run())


def test_mqtt_task_handles_incoming_message(
    monkeypatch: pytest.MonkeyPatch, runtime_config: RuntimeConfig
) -> None:
    async def _run() -> None:
        state = create_runtime_state(runtime_config)
        state.mqtt_topic_prefix = runtime_config.mqtt_topic
        service = _MQTTServiceStub(state)

        class _Inbound:
            def __init__(self, topic: str, payload: bytes) -> None:
                self.topic = topic
                self.payload = payload
                self.qos = 0
                self.retain = False
                self.response_topic = None
                self.correlation_data = None
                self.user_properties = ()
                self.content_type = None
                self.message_expiry_interval = None
                self.payload_format_indicator = None
                self.topic_alias = None

        messages: Deque[object] = deque(
            [_Inbound(f"{state.mqtt_topic_prefix}/console/in", b"hi")]
        )

        def _client_factory(*_: object, **__: object) -> _FakeMQTTClient:
            return _FakeMQTTClient(messages)

        monkeypatch.setattr("yunbridge.daemon.MQTTClient", _client_factory)

        async def _noop_connect(
            _config: RuntimeConfig, client: _FakeMQTTClient
        ) -> None:
            await client.connect()

        monkeypatch.setattr(
            "yunbridge.daemon._connect_mqtt_with_retry",
            _noop_connect,
        )

        monkeypatch.setattr(
            "yunbridge.daemon._build_mqtt_connect_properties",
            lambda: None,
        )

        task = asyncio.create_task(
            mqtt_task(runtime_config, state, cast(Any, service), None)
        )

        await asyncio.wait_for(service.handled.wait(), timeout=1)

        task.cancel()
        await asyncio.gather(task, return_exceptions=True)

        assert not messages

    asyncio.run(_run())
