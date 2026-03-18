"""Dispatcher helpers shared across McuBridge services."""

from __future__ import annotations

from collections.abc import Awaitable, Callable

from aiomqtt.message import Message
from mcubridge.protocol.topics import TopicRoute

McuHandler = Callable[[bytes], Awaitable[bool | None]]
MqttHandler = Callable[[TopicRoute, Message], Awaitable[bool]]


class MCUHandlerRegistry:
    """Registry that maps command identifiers to asyncio handlers."""

    def __init__(self) -> None:
        self._handlers: dict[int, McuHandler] = {}

    def register(self, command_id: int, handler: McuHandler) -> None:
        self._handlers[command_id] = handler

    def get(self, command_id: int) -> McuHandler | None:
        return self._handlers.get(command_id)


class MQTTRouter:
    """Topic-based dispatcher for inbound MQTT messages supporting wildcards (+, #)."""

    def __init__(self) -> None:
        self._handlers: list[tuple[str, MqttHandler]] = []

    def register(self, topic: str, handler: MqttHandler) -> None:
        """Register a handler for a topic pattern."""
        self._handlers.append((topic, handler))

    async def dispatch(
        self,
        route: TopicRoute,
        inbound: Message,
    ) -> bool:
        """Dispatch message to handlers matching the topic pattern."""
        for pattern, handler in self._handlers:
            # [SIL-2] Use aiomqtt's native wildcard matching for deterministic rounting
            if inbound.topic.matches(pattern):
                handled = await handler(route, inbound)
                if handled:
                    return True
        return False


__all__ = ["MCUHandlerRegistry", "MQTTRouter", "McuHandler", "MqttHandler"]
