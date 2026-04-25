"""Core dispatcher orchestrating message routing between MQTT and MCU. [SIL-2]"""

from __future__ import annotations

import asyncio
import collections
import contextlib
import structlog
from collections.abc import Callable, Coroutine
from typing import TYPE_CHECKING, Any, cast

import msgspec
import svcs
from aiomqtt.message import Message

from ..router.routers import MQTTRouter
from ..protocol.protocol import Status, Command, Topic
from ..protocol.structures import TopicRoute

if TYPE_CHECKING:
    from ..state.context import RuntimeState
    from .base import BridgeComponent

logger = structlog.get_logger("mcubridge.dispatcher")

McuHandler = Callable[[int, bytes], Coroutine[Any, Any, None]]


class BridgeDispatcher:
    """Orchestrates routing between the serial link and MQTT."""

    def __init__(
        self,
        mcu_registry: dict[int, McuHandler],
        mqtt_router: MQTTRouter,
        state: RuntimeState,
        send_frame: Callable[[int, bytes, int | None], Coroutine[Any, Any, bool]],
        acknowledge_frame: Callable[[int, int, Status], Coroutine[Any, Any, None]],
        is_topic_action_allowed: Callable[[Topic, str], bool],
        reject_topic_action: Callable[[Message, Topic, str], Coroutine[Any, Any, None]],
        publish_bridge_snapshot: Callable[[str, Message | None], Coroutine[Any, Any, None]],
        on_frame_received: Callable[[], None] | None = None,
    ) -> None:
        self.mcu_registry = mcu_registry
        self.mqtt_router = mqtt_router
        self.state = state
        self.send_frame = send_frame
        self.acknowledge_frame = acknowledge_frame
        self.is_topic_action_allowed = is_topic_action_allowed
        self.reject_topic_action = reject_topic_action
        self.publish_bridge_snapshot = publish_bridge_snapshot
        self.on_frame_received = on_frame_received
        self._container: svcs.Container | None = None

    def register_components(self, container: svcs.Container) -> None:
        """Register all component handlers with the registries. [Synchronous Retrieval]"""
        from . import (
            ConsoleComponent,
            DatastoreComponent,
            FileComponent,
            MailboxComponent,
            PinComponent,
            ProcessComponent,
            SpiComponent,
            SystemComponent,
        )

        self._container = container

        # [SIL-2] Type-safe component retrieval (Synchronous for test compatibility)
        console = container.get(ConsoleComponent)
        datastore = container.get(DatastoreComponent)
        file = container.get(FileComponent)
        mailbox = container.get(MailboxComponent)
        pin = container.get(PinComponent)
        process = container.get(ProcessComponent)
        spi = container.get(SpiComponent)
        system = container.get(SystemComponent)

        # MQTT Registry
        self.mqtt_router.register(Topic.CONSOLE, console.handle_mqtt)
        self.mqtt_router.register(Topic.DATASTORE, datastore.handle_mqtt)
        self.mqtt_router.register(Topic.FILE, file.handle_mqtt)
        self.mqtt_router.register(Topic.MAILBOX, mailbox.handle_mqtt)
        self.mqtt_router.register(Topic.DIGITAL, pin.handle_mqtt)
        self.mqtt_router.register(Topic.ANALOG, pin.handle_mqtt)
        self.mqtt_router.register(Topic.SHELL, process.handle_mqtt)
        self.mqtt_router.register(Topic.SPI, spi.handle_mqtt)
        self.mqtt_router.register(Topic.SYSTEM, system.handle_mqtt)

        # MCU Registry (Strict SIL-2 Naming Alignment)
        self.mcu_registry[Command.CMD_CONSOLE_WRITE.value] = console.handle_write
        self.mcu_registry[Command.CMD_DATASTORE_GET_RESP.value] = datastore.handle_get_request
        self.mcu_registry[Command.CMD_FILE_WRITE.value] = file.handle_write
        self.mcu_registry[Command.CMD_FILE_READ.value] = file.handle_read
        self.mcu_registry[Command.CMD_FILE_REMOVE.value] = file.handle_remove
        self.mcu_registry[Command.CMD_FILE_READ_RESP.value] = file.handle_read_response
        self.mcu_registry[Command.CMD_MAILBOX_PUSH.value] = mailbox.handle_push
        self.mcu_registry[Command.CMD_MAILBOX_READ.value] = mailbox.handle_read
        self.mcu_registry[Command.CMD_MAILBOX_PROCESSED.value] = mailbox.handle_processed
        self.mcu_registry[Command.CMD_MAILBOX_AVAILABLE.value] = mailbox.handle_available
        self.mcu_registry[Command.CMD_DIGITAL_READ_RESP.value] = pin.handle_mcu_digital_read
        self.mcu_registry[Command.CMD_ANALOG_READ_RESP.value] = pin.handle_mcu_analog_read
        self.mcu_registry[Command.CMD_PROCESS_RUN_ASYNC_RESP.value] = process.handle_run_async
        self.mcu_registry[Command.CMD_PROCESS_POLL_RESP.value] = process.handle_poll
        self.mcu_registry[Command.CMD_SPI_TRANSFER_RESP.value] = spi.handle_transfer_resp
        self.mcu_registry[Command.CMD_GET_FREE_MEMORY_RESP.value] = system.handle_get_free_memory_resp
        self.mcu_registry[Command.CMD_GET_VERSION_RESP.value] = system.handle_get_version_resp

    def register_system_handlers(self, **handlers: McuHandler) -> None:
        """Register core system handlers directly into the registry."""
        for cmd_name, handler in handlers.items():
            cmd_map = {
                "handle_link_sync_resp": Command.CMD_LINK_SYNC_RESP.value,
                "handle_link_reset_resp": Command.CMD_LINK_RESET_RESP.value,
                "handle_get_capabilities_resp": Command.CMD_GET_CAPABILITIES_RESP.value,
                "handle_ack": Status.ACK.value,
                "handle_process_kill": Command.CMD_PROCESS_KILL.value,
            }
            if cmd_name in cmd_map:
                self.mcu_registry[cmd_map[cmd_name]] = handler

        if "status_handler_factory" in handlers:
            factory = cast(Any, handlers["status_handler_factory"])
            for status in Status:
                self.mcu_registry[status.value] = factory(status)

    async def dispatch_mqtt_message(
        self,
        message: Message,
        parser: Callable[[str], TopicRoute | None],
    ) -> bool:
        """Parse and route an inbound MQTT message to its component handler."""
        topic_str = str(message.topic)
        route = parser(topic_str)
        if route is None:
            return False

        if not self.is_topic_action_allowed(route.topic, route.identifier):
            await self.reject_topic_action(message, route.topic, route.identifier)
            return False

        if route.topic == Topic.BRIDGE:
            await self._handle_bridge_topic(route, message)
            return True

        return await self.mqtt_router.dispatch(route, message)

    async def _handle_bridge_topic(self, route: TopicRoute, message: Message) -> None:
        """Handle daemon-specific topics (e.g. snapshots)."""
        if route.identifier == "snapshot" and route.segments[1:]:
            flavor = route.segments[1]
            await self.publish_bridge_snapshot(flavor, message)

    async def handle_mcu_frame(self, cmd_id: int, seq_id: int, payload: bytes) -> None:
        """Route an inbound MCU frame to its registered handler."""
        if self.on_frame_received:
            self.on_frame_received()

        handler = self.mcu_registry.get(cmd_id)
        if handler:
            try:
                await handler(seq_id, payload)
            except Exception as e:
                logger.error("Error in MCU frame handler (CMD:%02X): %s", cmd_id, e)
        else:
            self.state.unknown_command_count += 1
            self.state.unknown_command_last_id = cmd_id
            logger.warning("No handler registered for MCU command: %02X", cmd_id)
            await self.acknowledge_frame(cmd_id, seq_id, Status.NOT_IMPLEMENTED)
