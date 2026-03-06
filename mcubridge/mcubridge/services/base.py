"""Base interfaces for service components."""

from __future__ import annotations

from dataclasses import dataclass

from aiomqtt.message import Message

from ..config.settings import RuntimeConfig
from ..protocol.topics import TopicRoute
from ..state.context import RuntimeState


@dataclass(frozen=True)
class BridgeContext:
    """Context for a single MQTT command execution."""

    config: RuntimeConfig
    state: RuntimeState
    route: TopicRoute
    message: Message

    async def send_frame(self, command_id: int, payload: bytes = b"") -> bool:
        # This will be implemented by the service and passed here if needed,
        # but for now components call self.service.send_frame.
        return False


__all__ = ["BridgeContext"]
