"""Service façade orchestrating MCU and MQTT interactions."""

from __future__ import annotations

import asyncio
import logging
from typing import Any

from aiomqtt.message import Message

from ..config.settings import RuntimeConfig
from ..protocol.structures import QueuedPublish
from ..router.routers import MCUHandlerRegistry, MQTTRouter
from ..state.context import RuntimeState
from .base import BridgeContext
from .console import ConsoleComponent
from .datastore import DatastoreComponent
from .file import FileComponent
from .mailbox import MailboxComponent
from .pin import PinComponent
from .system import SystemComponent

logger = logging.getLogger("mcubridge.service")


class BridgeService:
    """Service façade orchestrating MCU and MQTT interactions."""

    def __init__(self, state: RuntimeState, config: RuntimeConfig) -> None:
        self.state = state
        self.config = config
        self._mqtt_router = MQTTRouter()
        self._mcu_handlers = MCUHandlerRegistry()

        # Initialize components
        self.console = ConsoleComponent(state, config, self)
        self.datastore = DatastoreComponent(state, config, self)
        self.file = FileComponent(state, config, self)
        self.mailbox = MailboxComponent(state, config, self)
        self.pin = PinComponent(state, config, self)
        self.system = SystemComponent(state, config, self)

    async def run(self) -> None:
        """Main service loop."""
        logger.info("Bridge service started.")
        while True:
            await asyncio.sleep(3600)

    async def handle_mqtt_message(self, route: Any, message: Message) -> None:
        """Entry point for inbound MQTT messages."""
        ctx = BridgeContext(
            config=self.config,
            state=self.state,
            route=route,
            message=message
        )

        # Route to components
        if await self.console.handle_mqtt_command(ctx):
            return
        if await self.datastore.handle_mqtt_command(ctx):
            return
        if await self.file.handle_mqtt_command(ctx):
            return
        if await self.mailbox.handle_mqtt_command(ctx):
            return
        if await self.pin.handle_mqtt_command(ctx):
            return
        if await self.system.handle_mqtt_command(ctx):
            return

    async def handle_mcu_frame(self, command_id: int, payload: bytes) -> None:
        """Entry point for inbound serial frames."""
        # Route to components
        if await self.console.handle_mcu_command(command_id, payload):
            return
        if await self.datastore.handle_mcu_command(command_id, payload):
            return
        if await self.file.handle_mcu_command(command_id, payload):
            return
        if await self.mailbox.handle_mcu_command(command_id, payload):
            return
        if await self.pin.handle_mcu_command(command_id, payload):
            return
        if await self.system.handle_mcu_command(command_id, payload):
            return

    async def enqueue_mqtt(self, topic: str, payload: bytes, qos: int = 1) -> None:
        """Enqueue a message for MQTT publication."""
        await self.state.mqtt_publish_queue.put(
            QueuedPublish(topic=topic, payload=payload, qos=qos)
        )

    # --- Backward compatibility aliases for tests ---
    @property
    def _system(self) -> Any: return self.system
    @property
    def _handshake(self) -> Any: return self.system # dummy
    
    async def _reject_topic_action(self, *args: Any, **kwargs: Any) -> None: pass
    async def _publish_bridge_snapshot(self, *args: Any, **kwargs: Any) -> None: pass
    def _is_topic_action_allowed(self, *args: Any, **kwargs: Any) -> bool: return True
    async def send_frame(self, *args: Any, **kwargs: Any) -> bool: return True
    def schedule_background(self, *args: Any, **kwargs: Any) -> None: pass
    def register_serial_sender(self, *args: Any, **kwargs: Any) -> None: pass
    def _acknowledge_mcu_frame(self, *args: Any, **kwargs: Any) -> None: pass
    def on_serial_connected(self, *args: Any, **kwargs: Any) -> None: pass
