"""Base interfaces for service components."""

from __future__ import annotations

import asyncio
import structlog
from collections.abc import Coroutine
from typing import Any, Protocol, TypeVar, TYPE_CHECKING

from aiomqtt.message import Message

from ..config.settings import RuntimeConfig
from ..state.context import RuntimeState

if TYPE_CHECKING:
    from ..protocol.structures import TopicRoute

TReq = TypeVar("TReq")

logger = structlog.get_logger("mcubridge.services")


class BridgeContext(Protocol):
    """Protocol describing the surface required by service components (SIL-2)."""

    config: RuntimeConfig
    state: RuntimeState

    @property
    def serial_flow(self) -> Any:
        """Access to the serial flow controller for sending frames."""
        ...

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


__all__ = ["BridgeContext", "BaseComponent"]
