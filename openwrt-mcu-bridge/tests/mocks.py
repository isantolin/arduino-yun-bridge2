"""Shared mocks for McuBridge tests."""

from __future__ import annotations

import asyncio
from collections import deque
from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable, Coroutine

from aiomqtt.message import Message
from mcubridge.config.settings import RuntimeConfig
from mcubridge.services.handshake import SerialHandshakeFatal
from mcubridge.state.context import RuntimeState


@dataclass
class MockSerialService:
    config: RuntimeConfig
    state: RuntimeState
    received_frames: deque[tuple[int, bytes]] = field(default_factory=deque)
    serial_connected: asyncio.Event = field(default_factory=asyncio.Event)
    serial_disconnected: asyncio.Event = field(default_factory=asyncio.Event)
    _serial_sender: Callable[[int, bytes], Awaitable[bool]] | None = None

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


class MockFatalSerialService(MockSerialService):
    async def on_serial_connected(self) -> None:
        raise SerialHandshakeFatal("fatal-handshake")


class MockMQTTService:
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
