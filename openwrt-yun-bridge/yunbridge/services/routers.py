"""Dispatcher helpers shared across YunBridge services."""
from __future__ import annotations

from collections.abc import Awaitable, Callable

from yunbridge.mqtt import InboundMessage
from yunbridge.protocol.topics import Topic, TopicRoute

McuHandler = Callable[[bytes], Awaitable[bool | None]]
MqttHandler = Callable[[TopicRoute, InboundMessage], Awaitable[bool]]


class MCUHandlerRegistry:
    """Registry that maps command identifiers to asyncio handlers."""

    def __init__(self) -> None:
        self._handlers: dict[int, McuHandler] = {}

    def register(self, command_id: int, handler: McuHandler) -> None:
        self._handlers[command_id] = handler

    def bulk_register(self, mapping: dict[int, McuHandler]) -> None:
        self._handlers.update(mapping)

    def get(self, command_id: int) -> McuHandler | None:
        return self._handlers.get(command_id)


class MQTTRouter:
    """Topic-based dispatcher for inbound MQTT messages."""

    def __init__(self) -> None:
        self._handlers: dict[Topic, list[MqttHandler]] = {}

    def register(self, topic: Topic, handler: MqttHandler) -> None:
        bucket = self._handlers.setdefault(topic, [])
        bucket.append(handler)

    async def dispatch(
        self,
        route: TopicRoute,
        inbound: InboundMessage,
    ) -> bool:
        for handler in self._handlers.get(route.topic, []):
            handled = await handler(route, inbound)
            if handled:
                return True
        return False


__all__ = ["MCUHandlerRegistry", "MQTTRouter", "McuHandler", "MqttHandler"]
