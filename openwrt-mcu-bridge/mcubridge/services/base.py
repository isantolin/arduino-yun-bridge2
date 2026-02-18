"""Base interfaces for service components."""

from __future__ import annotations

import asyncio
from collections.abc import Coroutine
from typing import Any, Protocol

from aiomqtt.message import Message

from ..config.settings import RuntimeConfig
from ..mqtt.messages import QueuedPublish
from ..state.context import RuntimeState



class BridgeContext(Protocol):
    """Protocol describing the surface required by service components."""

    config: RuntimeConfig
    state: RuntimeState

    async def send_frame(self, command_id: int, payload: bytes = b"") -> bool: ...

    async def enqueue_mqtt(
        self,
        message: QueuedPublish,
        *,
        reply_context: Message | None = None,
    ) -> None: ...

    async def publish(
        self,
        topic: str,
        payload: bytes | str,
        *,
        qos: int = 0,
        retain: bool = False,
        expiry: int | None = None,
        properties: tuple[tuple[str, str], ...] = (),
        content_type: str | None = None,
        reply_to: Message | None = None,
    ) -> None: ...

    async def schedule_background(
        self,
        coroutine: Coroutine[Any, Any, None],
        *,
        name: str | None = None,
    ) -> asyncio.Task[Any]: ...

__all__ = ["BridgeContext"]
