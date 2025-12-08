"""Base interfaces for service components."""
from __future__ import annotations

import asyncio
from collections.abc import Coroutine
from typing import Any, Protocol

from aiomqtt.client import Message as MQTTMessage
from ...mqtt.messages import QueuedPublish
from ...state.context import RuntimeState
from ...config.settings import RuntimeConfig


class BridgeContext(Protocol):
    """Protocol describing the surface required by service components."""

    config: RuntimeConfig
    state: RuntimeState

    async def send_frame(self, command_id: int, payload: bytes = b"") -> bool:
        ...

    async def enqueue_mqtt(
        self,
        message: QueuedPublish,
        *,
        reply_context: MQTTMessage | None = None,
    ) -> None:
        ...

    def is_command_allowed(self, command: str) -> bool:
        ...

    async def schedule_background(
        self,
        coroutine: Coroutine[Any, Any, None],
        *,
        name: str | None = None,
    ) -> asyncio.Task[Any]:
        ...


__all__ = ["BridgeContext"]
