"""Base interfaces for service components."""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable, Coroutine, Mapping
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

    def is_command_allowed(self, command: str) -> bool: ...

    async def schedule_background(
        self,
        coroutine: Coroutine[Any, Any, None],
        *,
        name: str | None = None,
    ) -> asyncio.Task[Any]: ...

async def dispatch_mqtt_action(
    action: str,
    handlers: Mapping[str, Callable[[], Awaitable[None]]],
    *,
    logger: logging.Logger,
    component: str,
) -> bool:
    handler = handlers.get(action)
    if handler is None:
        logger.debug("Ignoring %s action '%s'", component, action)
        return False
    await handler()
    return True


__all__ = ["BridgeContext", "dispatch_mqtt_action"]
