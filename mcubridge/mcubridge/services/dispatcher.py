"""Command dispatch logic for the MCU Bridge service."""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from typing import TYPE_CHECKING

import svcs

from mcubridge.protocol.contracts import response_to_request
from mcubridge.protocol.protocol import (
    Command,
    Status,
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
        console = container.get(ConsoleComponent)
        datastore = container.get(DatastoreComponent)
        file = container.get(FileComponent)
        mailbox = container.get(MailboxComponent)
        pin = container.get(PinComponent)
        process = container.get(ProcessComponent)
        spi = container.get(SpiComponent)
        system = container.get(SystemComponent)

        # Console
        self.mcu_registry[Command.CMD_XOFF.value] = console.handle_xoff
        self.mcu_registry[Command.CMD_XON.value] = console.handle_xon
        self.mcu_registry[Command.CMD_CONSOLE_WRITE.value] = console.handle_write
        self.mqtt_router.register(Topic.CONSOLE, console.handle_mqtt)

        # Datastore
        self.mcu_registry[Command.CMD_DATASTORE_PUT.value] = datastore.handle_put
        self.mcu_registry[Command.CMD_DATASTORE_GET.value] = datastore.handle_get_request
        self.mqtt_router.register(Topic.DATASTORE, datastore.handle_mqtt)

        # Mailbox
        self.mcu_registry[Command.CMD_MAILBOX_PUSH.value] = mailbox.handle_push
        self.mcu_registry[Command.CMD_MAILBOX_AVAILABLE.value] = mailbox.handle_available
        self.mcu_registry[Command.CMD_MAILBOX_READ.value] = mailbox.handle_read
        self.mcu_registry[Command.CMD_MAILBOX_PROCESSED.value] = mailbox.handle_processed
        self.mqtt_router.register(Topic.MAILBOX, mailbox.handle_mqtt)

        # File
        self.mcu_registry[Command.CMD_FILE_WRITE.value] = file.handle_write
        self.mcu_registry[Command.CMD_FILE_READ.value] = file.handle_read
        self.mcu_registry[Command.CMD_FILE_REMOVE.value] = file.handle_remove
        self.mcu_registry[Command.CMD_FILE_READ_RESP.value] = file.handle_read_response
        self.mqtt_router.register(Topic.FILE, file.handle_mqtt)

        # Process
        self.mcu_registry[Command.CMD_PROCESS_RUN_ASYNC.value] = process.handle_run_async
        self.mcu_registry[Command.CMD_PROCESS_POLL.value] = process.handle_poll

        # Shell (MQTT only - now handled by unified ProcessComponent)
        self.mqtt_router.register(Topic.SHELL, process.handle_mqtt)

        # Pin (GPIO)
        self.mcu_registry[Command.CMD_DIGITAL_READ_RESP.value] = pin.handle_digital_read_resp
        self.mcu_registry[Command.CMD_ANALOG_READ_RESP.value] = pin.handle_analog_read_resp

        async def _handle_mcu_read(s: int, cmd: Command, p: bytes) -> bool:
            if self._container:
                from . import PinComponent

                pin_cmp = self._container.get(PinComponent)
                await pin_cmp.handle_unexpected_mcu_request(s, cmd, p)
                return True
            logger.warning(
                "Pin component not registered; dropping unexpected %s", cmd.name
            )
            return False

        self.mcu_registry[Command.CMD_DIGITAL_READ.value] = (
            lambda s, p: _handle_mcu_read(s, Command.CMD_DIGITAL_READ, p)
        )
        self.mcu_registry[Command.CMD_ANALOG_READ.value] = (
            lambda s, p: _handle_mcu_read(s, Command.CMD_ANALOG_READ, p)
        )

        self.mqtt_router.register(Topic.DIGITAL, pin.handle_mqtt)
        self.mqtt_router.register(Topic.ANALOG, pin.handle_mqtt)

        # SPI
        self.mcu_registry[Command.CMD_SPI_TRANSFER_RESP.value] = spi.handle_transfer_resp
        self.mqtt_router.register(Topic.SPI, spi.handle_mqtt)

        # System
        self.mcu_registry[Command.CMD_GET_FREE_MEMORY_RESP.value] = system.handle_get_free_memory_resp
        self.mcu_registry[Command.CMD_GET_VERSION_RESP.value] = system.handle_get_version_resp
        self.mqtt_router.register(Topic.SYSTEM, self._handle_system_topic)

    def register_system_handlers(
        self,
        handle_link_sync_resp: Callable[[int, bytes], Awaitable[bool]],
        handle_link_reset_resp: Callable[[int, bytes], Awaitable[bool]],
        handle_get_capabilities_resp: Callable[[int, bytes], Awaitable[bool]],
        handle_ack: Callable[[int, bytes], Awaitable[None]],
        status_handler_factory: Callable[
            [Status], Callable[[int, bytes], Awaitable[None]]
        ],
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

    async def dispatch_mcu_frame(
        self, command_id: int, sequence_id: int, payload: bytes
    ) -> None:
        """
        Route an incoming frame from the MCU to the appropriate registered handler.
        """
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
            logger.debug(
                "MCU > %s (seq=%d) [%d bytes]", command_name, sequence_id, len(payload)
            )
            try:
                result = await handler(sequence_id, payload)
                handled_successfully = result is not False
            except (
                OSError,
                ValueError,
                TypeError,
                AttributeError,
                KeyError,
                IndexError,
                RuntimeError,
            ) as exc:
                logger.critical(
                    "Critical: Exception in handler for %s: %s", command_name, exc
                )
                if response_to_request(command_id) is None:
                    await self.send_frame(Status.ERROR.value, b"Internal Error")

        elif response_to_request(command_id) is None:
            logger.warning("Protocol: Unhandled MCU command %s", command_name)
            self.state.record_unknown_command_id(command_id)
            await self.send_frame(Status.NOT_IMPLEMENTED.value, b"")

        if handled_successfully and command_id not in STATUS_VALUES:
            await self.acknowledge_frame(command_id, sequence_id)

    async def dispatch_mqtt_message(
        self,
        inbound: Message,
        parse_topic_func: Callable[[str], TopicRoute | None],
    ) -> None:
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
        try:
            if not await self.mqtt_router.dispatch(route, inbound):
                logger.debug("Unhandled MQTT topic %s", inbound_topic)
        except (
            ValueError,
            TypeError,
            AttributeError,
            KeyError,
            IndexError,
            RuntimeError,
        ):
            logger.exception("Fault Isolation: MQTT processing failed for %s", inbound_topic)

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
        return (
            self.state.is_synchronized
            or command_id in STATUS_VALUES
            or command_id in _PRE_SYNC_ALLOWED_COMMANDS
        )

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
