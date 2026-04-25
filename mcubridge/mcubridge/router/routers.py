"""Dispatcher helpers shared across McuBridge services."""

from __future__ import annotations

from collections.abc import Awaitable, Callable

from typing import Any

from aiomqtt.message import Message
from mcubridge.protocol.topics import Topic, TopicRoute

McuHandler = Callable[[int, Any], Awaitable[bool | None]]
MqttHandler = Callable[[TopicRoute, Message], Awaitable[bool]]


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
        inbound: Message,
    ) -> bool:
        dispatched = False
        for handler in self._handlers.get(route.topic, []):
            if await handler(route, inbound):
                dispatched = True
        return dispatched


__all__ = ["MQTTRouter", "McuHandler", "MqttHandler"]
