"""Base interfaces for service components."""
from __future__ import annotations

from typing import Any, Coroutine, Protocol

from ...mqtt import PublishableMessage
from ...state.context import RuntimeState
from ...config.settings import RuntimeConfig


class BridgeContext(Protocol):
    """Protocol describing the surface required by service components."""

    config: RuntimeConfig
    state: RuntimeState

    async def send_frame(self, command_id: int, payload: bytes = b"") -> bool:
        ...

    async def enqueue_mqtt(self, message: PublishableMessage) -> None:
        ...

    def is_command_allowed(self, command: str) -> bool:
        ...

    def schedule_background(
        self, coroutine: Coroutine[Any, Any, None]
    ) -> None:
        ...


__all__ = ["BridgeContext"]
