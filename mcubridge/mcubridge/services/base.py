"""Base interfaces for service components."""

from __future__ import annotations

import asyncio
import structlog
from collections.abc import Coroutine
from typing import Any, Protocol, TypeVar, TYPE_CHECKING

from aiomqtt.message import Message

from ..config.settings import RuntimeConfig
from ..protocol.structures import QueuedPublish
from ..state.context import RuntimeState

if TYPE_CHECKING:
    from ..protocol.structures import TopicRoute

TReq = TypeVar("TReq")

logger = structlog.get_logger("mcubridge.services")


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

    async def acknowledge_mcu_frame(
        self,
        command_id: int,
        seq_id: int,
        *,
        status: Any = None,
    ) -> None: ...

    async def schedule_background(
        self,
        coroutine: Coroutine[Any, Any, None],
        *,
        name: str | None = None,
    ) -> asyncio.Task[Any]: ...


class BaseComponent:
    """Base class for services providing shared configuration and context."""

    def __init__(
        self, config: RuntimeConfig, state: RuntimeState, ctx: BridgeContext
    ) -> None:
        self.config = config
        self.state = state
        self.ctx = ctx

    async def handle_mqtt(self, route: TopicRoute, inbound: Message) -> bool:
        """Handle an inbound MQTT message routed to this service."""
        return False

    async def _publish_value(
        self,
        topic: str,
        payload: bytes | str,
        expiry: int,
        reply_context: Message | None = None,
        content_type: str = "text/plain; charset=utf-8",
        properties: tuple[tuple[str, str], ...] = (),
    ) -> None:
        """Centralized helper for broadcasting a value and sending a targeted reply."""
        # Broadcast to all subscribers
        await self.ctx.publish(
            topic=topic,
            payload=payload,
            expiry=expiry,
            content_type=content_type,
            reply_to=None,
            properties=properties,
        )
        # Targeted reply if context exists
        if reply_context is not None:
            await self.ctx.publish(
                topic=topic,
                payload=payload,
                expiry=expiry,
                content_type=content_type,
                reply_to=reply_context,
                properties=properties,
            )

    def _decode_payload(
        self, packet_cls: Any, payload: bytes, command_id: Any
    ) -> Any | None:
        """Safely decode an RPC payload using the provided packet class."""
        try:
            return packet_cls.decode(payload, command_id)
        except ValueError:
            logger.warning(
                "Malformed %s payload: %s", packet_cls.__name__, payload.hex()
            )
            return None


__all__ = ["BridgeContext", "BaseComponent"]
