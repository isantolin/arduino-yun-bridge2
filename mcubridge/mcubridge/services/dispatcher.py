"""Command dispatch logic for the MCU Bridge service."""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from typing import TYPE_CHECKING

import svcs

from mcubridge.protocol.protocol import (
    Command,
    Status,
    response_to_request,
)
from mcubridge.protocol.topics import Topic, TopicRoute
from mcubridge.state.context import RuntimeState, resolve_command_id

from ..router.routers import MQTTRouter
import structlog

if TYPE_CHECKING:
    from aiomqtt import Message
    from ..router.routers import McuHandler

logger = structlog.get_logger("mcubridge.dispatcher")

STATUS_VALUES = {status.value for status in Status}
_PRE_SYNC_ALLOWED_COMMANDS = {
    Command.CMD_LINK_SYNC_RESP.value,
    Command.CMD_LINK_RESET_RESP.value,
}


class BridgeDispatcher:
    """Orchestrates message routing between Serial and MQTT layers."""

    def __init__(
        self,
        mcu_registry: dict[int, McuHandler],
        mqtt_router: MQTTRouter,
        state: RuntimeState,
        send_frame: Callable[[int, bytes], Awaitable[bool]],
        acknowledge_frame: Callable[[int, int], Awaitable[None]],
        is_topic_action_allowed: Callable[[Topic, str], bool],
        reject_topic_action: Callable[[Message, Topic, str], Awaitable[None]],
        publish_bridge_snapshot: Callable[[str, Message | None], Awaitable[None]],
        on_frame_received: Callable[[int, int, bytes], None] | None = None,
    ) -> None:
        self.mcu_registry = mcu_registry
        self.mqtt_router = mqtt_router
        self.state = state
        self.send_frame = send_frame
        self.acknowledge_frame = acknowledge_frame
        self.is_topic_action_allowed = is_topic_action_allowed
        self.reject_topic_action = reject_topic_action
        self.publish_bridge_snapshot = publish_bridge_snapshot

        self.on_frame_received_callback = on_frame_received

        self._container: svcs.Container | None = None

    def register_components(self, container: svcs.Container) -> None:
        """Register all component handlers with the registries."""
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

        # [SIL-2] Type-safe component retrieval
        console = container.get(ConsoleComponent)
        datastore = container.get(DatastoreComponent)
        file = container.get(FileComponent)
        mailbox = container.get(MailboxComponent)
        pin = container.get(PinComponent)
        process = container.get(ProcessComponent)
        spi = container.get(SpiComponent)
        system = container.get(SystemComponent)

        # MCU Command Dispatch Map (Centralized for auditability)
        mcu_map: dict[Command, McuHandler] = {
            Command.CMD_XOFF: console.handle_xoff,
            Command.CMD_XON: console.handle_xon,
            Command.CMD_CONSOLE_WRITE: console.handle_write,
            Command.CMD_DATASTORE_PUT: datastore.handle_put,
            Command.CMD_DATASTORE_GET: datastore.handle_get_request,
            Command.CMD_MAILBOX_PUSH: mailbox.handle_push,
            Command.CMD_MAILBOX_AVAILABLE: mailbox.handle_available,
            Command.CMD_MAILBOX_READ: mailbox.handle_read,
            Command.CMD_MAILBOX_PROCESSED: mailbox.handle_processed,
            Command.CMD_FILE_WRITE: file.handle_write,
            Command.CMD_FILE_READ: file.handle_read,
            Command.CMD_FILE_REMOVE: file.handle_remove,
            Command.CMD_FILE_READ_RESP: file.handle_read_response,
            Command.CMD_PROCESS_RUN_ASYNC: process.handle_run_async,
            Command.CMD_PROCESS_POLL: process.handle_poll,
            Command.CMD_DIGITAL_READ_RESP: pin.handle_digital_read_resp,
            Command.CMD_ANALOG_READ_RESP: pin.handle_analog_read_resp,
            Command.CMD_DIGITAL_READ: pin.handle_mcu_digital_read,
            Command.CMD_ANALOG_READ: pin.handle_mcu_analog_read,
            Command.CMD_SPI_TRANSFER_RESP: spi.handle_transfer_resp,
            Command.CMD_GET_FREE_MEMORY_RESP: system.handle_get_free_memory_resp,
            Command.CMD_GET_VERSION_RESP: system.handle_get_version_resp,
        }
        for cmd, handler in mcu_map.items():
            self.mcu_registry[cmd.value] = handler

        # MQTT Topic Dispatch Map
        mqtt_map = {
            Topic.CONSOLE: console.handle_mqtt,
            Topic.DATASTORE: datastore.handle_mqtt,
            Topic.MAILBOX: mailbox.handle_mqtt,
            Topic.FILE: file.handle_mqtt,
            Topic.SHELL: process.handle_mqtt,
            Topic.DIGITAL: pin.handle_mqtt,
            Topic.ANALOG: pin.handle_mqtt,
            Topic.SPI: spi.handle_mqtt,
            Topic.SYSTEM: self._handle_system_topic,
        }
        for topic, handler in mqtt_map.items():
            self.mqtt_router.register(topic, handler)
    def register_system_handlers(
        self,
        handle_link_sync_resp: Callable[[int, bytes], Awaitable[bool]],
        handle_link_reset_resp: Callable[[int, bytes], Awaitable[bool]],
        handle_get_capabilities_resp: Callable[[int, bytes], Awaitable[bool]],
        handle_ack: Callable[[int, bytes], Awaitable[None]],
        status_handler_factory: Callable[[Status], Callable[[int, bytes], Awaitable[None]]],
        handle_process_kill: Callable[[int, bytes], Awaitable[bool | None]],
    ) -> None:
        self.mcu_registry[Command.CMD_LINK_SYNC_RESP.value] = handle_link_sync_resp
        self.mcu_registry[Command.CMD_LINK_RESET_RESP.value] = handle_link_reset_resp
        self.mcu_registry[Command.CMD_GET_CAPABILITIES_RESP.value] = handle_get_capabilities_resp
        self.mcu_registry[Command.CMD_PROCESS_KILL.value] = handle_process_kill

        self.mcu_registry[Status.ACK.value] = handle_ack
        for status in Status:
            if status == Status.ACK:
                continue
            self.mcu_registry[status.value] = status_handler_factory(status)

    async def dispatch_mcu_frame(self, command_id: int, sequence_id: int, payload: bytes) -> None:
        """
        Route an incoming frame from the MCU to the appropriate registered handler.
        """
        now = asyncio.get_running_loop().time()
        try:
            if self.on_frame_received_callback:
                self.on_frame_received_callback(command_id, sequence_id, payload)

            if not self._is_frame_allowed_pre_sync(command_id):
                logger.warning(
                    "Security: Rejecting MCU frame 0x%02X (Link not synchronized)",
                    command_id,
                )
                return

            handler = self.mcu_registry.get(command_id)
            command_name = resolve_command_id(command_id)
            handled_successfully = False

            if handler:
                logger.debug("MCU > %s (seq=%d) [%d bytes]", command_name, sequence_id, len(payload))
                handled_successfully = (await handler(sequence_id, payload)) is not False

            elif response_to_request(command_id) is None:
                logger.warning("Protocol: Unhandled MCU command %s", command_name)
                # [SIL-2] Direct metrics recording (No Wrapper)
                self.state.unknown_command_count += 1
                self.state.metrics.unknown_command_count.inc()
                self.state.unknown_command_last_id = command_id
                await self.send_frame(Status.NOT_IMPLEMENTED.value, b"")

            if handled_successfully and command_id not in STATUS_VALUES:
                await self.acknowledge_frame(command_id, sequence_id)
        finally:
            latency_ms = (asyncio.get_running_loop().time() - now) * 1000.0
            self.state.serial_latency_stats.record(latency_ms)
            self.state.metrics.serial_latency_ms.observe(latency_ms)



    async def dispatch_mqtt_message(
        self,
        inbound: Message,
        parse_topic_func: Callable[[str], TopicRoute | None],
    ) -> None:
        start = asyncio.get_running_loop().time()
        try:
            inbound_topic = str(inbound.topic)
            route = parse_topic_func(inbound_topic)
            if route is None or not route.segments:
                return

            # 1. Policy Guard (Eradicated _guard_and_dispatch wrapper)
            if action := self._get_topic_action(route):
                if not self.is_topic_action_allowed(route.topic, action):
                    await self.reject_topic_action(inbound, route.topic, action)
                    return

            # 2. Synchronization Guard
            if route.topic != Topic.SYSTEM:
                try:
                    async with asyncio.timeout(30.0):
                        await self.state.link_sync_event.wait()
                except asyncio.TimeoutError:
                    logger.warning("MQTT > Link sync timeout for %s", inbound_topic)
                    return

            # 3. Router Dispatch
            if not await self.mqtt_router.dispatch(route, inbound):
                logger.debug("Unhandled MQTT topic %s", inbound_topic)
        finally:
            latency_ms = (asyncio.get_running_loop().time() - start) * 1000.0
            # [SIL-2] Direct metrics recording (No Wrapper)
            self.state.rpc_latency_stats.record(latency_ms)
            self.state.metrics.rpc_latency_ms.observe(latency_ms)

    def _get_topic_action(self, route: TopicRoute) -> str | None:
        """Deduce the action name for policy enforcement from the route."""
        match route.topic:
            case Topic.SYSTEM:
                return None
            case Topic.DIGITAL | Topic.ANALOG:
                if not route.segments:
                    return None
                return "write" if len(route.segments) == 1 else (route.segments[1].lower() or None)
            case Topic.CONSOLE:
                return "in" if route.identifier == "in" else None
            case _:
                return route.identifier or None

    def _is_frame_allowed_pre_sync(self, command_id: int) -> bool:
        return self.state.is_synchronized or command_id in STATUS_VALUES or command_id in _PRE_SYNC_ALLOWED_COMMANDS

    async def _handle_system_topic(self, route: TopicRoute, inbound: Message) -> bool:
        match route.identifier:
            case "bridge":
                return await self._handle_bridge_topic(route, inbound)
            case _:
                if self._container:
                    from . import SystemComponent

                    return await self._container.get(SystemComponent).handle_mqtt(route, inbound)
        return False

    async def _handle_bridge_topic(self, route: TopicRoute, inbound: Message) -> bool:
        match list(route.remainder):
            case ["handshake", "get"]:
                await self.publish_bridge_snapshot("handshake", inbound)
                return True
            case [("summary" | "state"), "get"]:
                await self.publish_bridge_snapshot("summary", inbound)
                return True
            case _:
                return False
        return False
