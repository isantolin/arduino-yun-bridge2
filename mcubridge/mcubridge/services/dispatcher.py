"""Command dispatch logic for the MCU Bridge service."""

from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable
from typing import TYPE_CHECKING

from mcubridge.protocol.topics import TopicRoute
from mcubridge.state.context import RuntimeState

from ..router.routers import MCUHandlerRegistry, MQTTRouter
from .base import BridgeContext

if TYPE_CHECKING:
    from aiomqtt.message import Message
    from .runtime import BridgeService

logger = logging.getLogger("mcubridge.dispatcher")

McuHandler = Callable[[bytes], Awaitable[bool | None]]
MqttHandler = Callable[[BridgeContext], Awaitable[bool]]


class Dispatcher:
    """Orchestrates message routing between Serial and MQTT."""

    def __init__(self, state: RuntimeState, service: BridgeService) -> None:
        self.state = state
        self.service = service
        self.mqtt_router = MQTTRouter()
        self.mcu_handlers = MCUHandlerRegistry()
        self._setup_routes()

    def _setup_routes(self) -> None:
        """Initialize routing tables."""
        # 1. MCU -> MQTT (Serial responses)
        # ... implementation ...
        pass

    async def handle_mcu_frame(self, command_id: int, payload: bytes) -> None:
        """Dispatch a frame received from the MCU."""
        handler = self.mcu_handlers.get(command_id)
        if handler:
            await handler(payload)
        else:
            logger.warning("No handler for MCU command 0x%02X", command_id)

    async def handle_mqtt_message(self, route: TopicRoute, message: Message) -> None:
        """Dispatch a message received from MQTT."""
        BridgeContext(
            config=self.service.config,
            state=self.state,
            route=route,
            message=message
        )
        # Logic to route to components...
        await self.service.handle_mqtt_message(route, message)
BridgeDispatcher = Dispatcher
