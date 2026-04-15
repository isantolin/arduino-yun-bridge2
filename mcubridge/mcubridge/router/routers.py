"""Dispatcher helpers shared across McuBridge services."""

from __future__ import annotations

from collections.abc import Awaitable, Callable

import structlog
from aiomqtt.message import Message
from mcubridge.protocol.topics import Topic, TopicRoute

McuHandler = Callable[[int, bytes], Awaitable[bool | None]]
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
            try:
                handled = await handler(route, inbound)
                if handled:
                    dispatched = True
            except (
                OSError,
                ValueError,
                TypeError,
                AttributeError,
                KeyError,
                IndexError,
                RuntimeError,
            ):
                # [SIL-2] Fault Isolation: Don't let one handler crash the whole router.
                # We log it and continue with the next one.
                structlog.get_logger("mcubridge.router").exception(
                    "Fault Isolation: Handler failed for topic %s", route.topic
                )
        return dispatched


__all__ = ["MQTTRouter", "McuHandler", "MqttHandler"]
