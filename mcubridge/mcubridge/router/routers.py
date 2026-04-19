"""Dispatcher helpers shared across McuBridge services."""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from typing import Any

from aiomqtt.message import Message
from mcubridge.protocol.topics import Topic, TopicRoute

McuHandler = Callable[[int, bytes], Awaitable[bool | None]]
MqttHandler = Callable[[TopicRoute, Message], Awaitable[bool]]


class MQTTRouter:
    """Topic and Action based dispatcher for inbound MQTT messages (SIL-2)."""

    def __init__(self) -> None:
        # Two-level lookup: Topic -> Action -> Handlers
        # Action can be a specific Enum value, a string, or None for 'any/no action'.
        self._handlers: dict[Topic, dict[Any, list[MqttHandler]]] = {}

    def register(
        self, topic: Topic, handler: MqttHandler, action: Any | None = None
    ) -> None:
        """Register a handler for a specific topic and optional action."""
        topic_bucket = self._handlers.setdefault(topic, {})
        action_bucket = topic_bucket.setdefault(action, [])
        action_bucket.append(handler)

    async def dispatch(
        self,
        route: TopicRoute,
        inbound: Message,
    ) -> bool:
        """Route message to all matching handlers based on topic and action."""
        dispatched = False
        topic_bucket = self._handlers.get(route.topic, {})
        if not topic_bucket:
            return False

        # 1. Specific action handlers
        target_action = route.action
        for handler in topic_bucket.get(target_action, []):
            if await handler(route, inbound):
                dispatched = True

        # 2. Generic topic handlers (action=None)
        if target_action is not None:
            for handler in topic_bucket.get(None, []):
                if await handler(route, inbound):
                    dispatched = True

        return dispatched


__all__ = ["MQTTRouter", "McuHandler", "MqttHandler"]
