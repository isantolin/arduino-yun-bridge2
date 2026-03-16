"""Command dispatch logic for the MCU Bridge service."""

from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable
from typing import TYPE_CHECKING, Any

import svcs

from mcubridge.protocol.contracts import response_to_request
from mcubridge.protocol.protocol import (
    Command,
    Status,
)
from mcubridge.protocol.topics import Topic, TopicRoute
from mcubridge.state.context import RuntimeState, resolve_command_id

from ..router.routers import MCUHandlerRegistry, MQTTRouter

if TYPE_CHECKING:
    from aiomqtt import Message

    from . import (
        ConsoleComponent,
        DatastoreComponent,
        FileComponent,
        MailboxComponent,
        PinComponent,
        ProcessComponent,
        ShellComponent,
        SystemComponent,
    )

logger = logging.getLogger("mcubridge.dispatcher")

STATUS_VALUES = {status.value for status in Status}
_PRE_SYNC_ALLOWED_COMMANDS = {
    Command.CMD_LINK_SYNC_RESP.value,
    Command.CMD_LINK_RESET_RESP.value,
}


class BridgeDispatcher:
    """Orchestrates message routing between Serial and MQTT layers."""

    def __init__(
        self,
        mcu_registry: MCUHandlerRegistry,
        mqtt_router: MQTTRouter,
        state: RuntimeState,
        send_frame: Callable[[int, bytes], Awaitable[bool]],
        acknowledge_frame: Callable[[int], Awaitable[None]],
        is_topic_action_allowed: Callable[[Topic, str], bool],
        reject_topic_action: Callable[[Message, Topic, str], Awaitable[None]],
        publish_bridge_snapshot: Callable[[str, Message | None], Awaitable[None]],
        on_frame_received: Callable[[int, bytes], None] | None = None,
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
            ShellComponent,
            SystemComponent,
        )

        self._container = container
        console = container.get(ConsoleComponent)
        datastore = container.get(DatastoreComponent)
        file = container.get(FileComponent)
        mailbox = container.get(MailboxComponent)
        pin = container.get(PinComponent)
        process = container.get(ProcessComponent)
        shell = container.get(ShellComponent)
        system = container.get(SystemComponent)

        # Console
        self.mcu_registry.register(Command.CMD_XOFF.value, console.handle_xoff)
        self.mcu_registry.register(Command.CMD_XON.value, console.handle_xon)
        self.mcu_registry.register(Command.CMD_CONSOLE_WRITE.value, console.handle_write)
        self.mqtt_router.register(
            Topic.CONSOLE, lambda r, m: self._guard_and_dispatch(r, m, console.handle_mqtt_input)
        )

        # Datastore
        self.mcu_registry.register(Command.CMD_DATASTORE_PUT.value, datastore.handle_put)
        self.mcu_registry.register(Command.CMD_DATASTORE_GET.value, datastore.handle_get_request)
        self.mqtt_router.register(
            Topic.DATASTORE,
            lambda r, m: self._guard_and_dispatch(
                r,
                m,
                lambda p, i: datastore.handle_mqtt(
                    r.identifier, list(r.remainder), p, p.decode("utf-8", errors="ignore"), i
                ),
            ),
        )

        # Mailbox
        self.mcu_registry.register(Command.CMD_MAILBOX_PUSH.value, mailbox.handle_push)
        self.mcu_registry.register(Command.CMD_MAILBOX_AVAILABLE.value, mailbox.handle_available)
        self.mcu_registry.register(Command.CMD_MAILBOX_READ.value, mailbox.handle_read)
        self.mcu_registry.register(Command.CMD_MAILBOX_PROCESSED.value, mailbox.handle_processed)
        self.mqtt_router.register(
            Topic.MAILBOX,
            lambda r, m: self._guard_and_dispatch(r, m, lambda p, i: mailbox.handle_mqtt(r.identifier, p, i)),
        )

        # File
        self.mcu_registry.register(Command.CMD_FILE_WRITE.value, file.handle_write)
        self.mcu_registry.register(Command.CMD_FILE_READ.value, file.handle_read)
        self.mcu_registry.register(Command.CMD_FILE_REMOVE.value, file.handle_remove)

        async def file_mqtt_handler(r: TopicRoute, m: Message) -> bool:
            if len(r.segments) < 2:
                return False
            return await self._guard_and_dispatch(r, m, lambda _p, _i: file.handle_mqtt(r, m))

        self.mqtt_router.register(Topic.FILE, file_mqtt_handler)

        # Process
        self.mcu_registry.register(
            Command.CMD_PROCESS_RUN_ASYNC.value,
            process.handle_run_async,
        )
        self.mcu_registry.register(Command.CMD_PROCESS_POLL.value, process.handle_poll)

        # Shell (MQTT only)
        self.mqtt_router.register(
            Topic.SHELL,
            lambda r, m: self._guard_and_dispatch(r, m, lambda p, i: shell.handle_mqtt(list(r.segments), p, i)),
        )

        # Pin (GPIO)
        self.mcu_registry.register(Command.CMD_DIGITAL_READ_RESP.value, pin.handle_digital_read_resp)
        self.mcu_registry.register(Command.CMD_ANALOG_READ_RESP.value, pin.handle_analog_read_resp)

        async def _handle_mcu_read(cmd: Command, p: bytes) -> bool:
            if self._container:
                from . import PinComponent

                pin_cmp = self._container.get(PinComponent)
                await pin_cmp.handle_unexpected_mcu_request(cmd, p)
                return True
            logger.warning("Pin component not registered; dropping unexpected %s", cmd.name)
            return False

        self.mcu_registry.register(
            Command.CMD_DIGITAL_READ.value, lambda p: _handle_mcu_read(Command.CMD_DIGITAL_READ, p)
        )
        self.mcu_registry.register(
            Command.CMD_ANALOG_READ.value, lambda p: _handle_mcu_read(Command.CMD_ANALOG_READ, p)
        )

        async def pin_mqtt_handler(r: TopicRoute, m: Message) -> bool:
            return await self._guard_and_dispatch(
                r,
                m,
                lambda p, i: pin.handle_mqtt(r.topic, list(r.segments), p.decode("utf-8", errors="ignore"), i),
            )

        self.mqtt_router.register(Topic.DIGITAL, pin_mqtt_handler)
        self.mqtt_router.register(Topic.ANALOG, pin_mqtt_handler)

        # System
        self.mcu_registry.register(
            Command.CMD_GET_FREE_MEMORY_RESP.value,
            system.handle_get_free_memory_resp,
        )
        self.mcu_registry.register(
            Command.CMD_GET_VERSION_RESP.value,
            system.handle_get_version_resp,
        )
        self.mcu_registry.register(
            Command.CMD_SET_BAUDRATE_RESP.value,
            system.handle_set_baudrate_resp,
        )
        self.mqtt_router.register(Topic.SYSTEM, self._handle_system_topic)

    def register_system_handlers(
        self,
        handle_link_sync_resp: Callable[[bytes], Awaitable[bool]],
        handle_link_reset_resp: Callable[[bytes], Awaitable[bool]],
        handle_get_capabilities_resp: Callable[[bytes], Awaitable[bool]],
        handle_ack: Callable[[bytes], Awaitable[None]],
        status_handler_factory: Callable[[Status], Callable[[bytes], Awaitable[None]]],
        handle_process_kill: Callable[[bytes], Awaitable[bool | None]],
    ) -> None:
        self.mcu_registry.register(Command.CMD_LINK_SYNC_RESP.value, handle_link_sync_resp)
        self.mcu_registry.register(Command.CMD_LINK_RESET_RESP.value, handle_link_reset_resp)
        self.mcu_registry.register(Command.CMD_GET_CAPABILITIES_RESP.value, handle_get_capabilities_resp)
        self.mcu_registry.register(Command.CMD_PROCESS_KILL.value, handle_process_kill)

        self.mcu_registry.register(Status.ACK.value, handle_ack)
        for status in Status:
            if status == Status.ACK:
                continue
            self.mcu_registry.register(status.value, status_handler_factory(status))

    async def dispatch_mcu_frame(self, command_id: int, payload: bytes) -> None:
        """
        Route an incoming frame from the MCU to the appropriate registered handler.

        This method acts as a Firewall/Router. It enforces pre-sync validation
        and wraps handler execution in a safety try/except block.
        """
        # 0. Notify Flow Controller (if registered)
        if self.on_frame_received_callback:
            self.on_frame_received_callback(command_id, payload)

        # 1. Security Check: Link Synchronization
        if not self._is_frame_allowed_pre_sync(command_id):
            logger.warning(
                "Security: Rejecting MCU frame 0x%02X (Link not synchronized)",
                command_id,
            )
            return

        # 2. Handler Resolution
        handler = self.mcu_registry.get(command_id)
        command_name = resolve_command_id(command_id)

        # 3. Safe Execution Strategy
        handled_successfully = False

        if handler:
            logger.debug("MCU > %s [%d bytes]", command_name, len(payload))
            try:
                result = await handler(payload)
                handled_successfully = result is not False
            except (
                OSError, ValueError, TypeError, AttributeError, KeyError, IndexError, RuntimeError
            ) as exc:
                logger.critical(
                    "Critical: Exception in handler for command %s: %s",
                    command_name, exc, exc_info=True
                )
                if response_to_request(command_id) is None:
                    await self.send_frame(Status.ERROR.value, b"Internal Error")

        elif response_to_request(command_id) is None:
            logger.warning(
                "Protocol: Unhandled MCU command %s (No handler registered)",
                command_name,
            )
            self.state.record_unknown_command_id(command_id)
            await self.send_frame(Status.NOT_IMPLEMENTED.value, b"")
        else:
            logger.debug("Protocol: Ignoring orphaned MCU response %s", command_name)

        # 4. Auto-Acknowledgement (if applicable)
        if handled_successfully and command_id not in STATUS_VALUES:
            await self.acknowledge_frame(command_id)

    async def dispatch_mqtt_message(
        self,
        inbound: Message,
        parse_topic_func: Callable[[str], TopicRoute | None],
    ) -> None:
        inbound_topic = str(inbound.topic)
        route = parse_topic_func(inbound_topic)
        if route is None or not route.segments:
            logger.debug("Ignoring MQTT message: %s", inbound_topic)
            return

        try:
            handled = await self.mqtt_router.dispatch(route, inbound)
        except (
            OSError, ValueError, TypeError, AttributeError, KeyError, IndexError, RuntimeError
        ):
            logger.exception("Error processing MQTT topic: %s", inbound_topic)
            return

        if not handled:
            logger.debug("Unhandled MQTT topic %s", inbound_topic)

    async def _guard_and_dispatch(
        self,
        route: TopicRoute,
        inbound: Message,
        handler: Callable[[bytes, Message], Awaitable[Any]],
    ) -> bool:
        """Enforces policy, coerces payload, and executes handler."""
        if action := self._should_reject_topic_action(route):
            await self.reject_topic_action(inbound, route.topic, action)
            return True

        payload = self._payload_bytes(inbound.payload)
        await handler(payload, inbound)
        return True

    def _should_reject_topic_action(self, route: TopicRoute) -> str | None:
        """Deduce if an MQTT route should be rejected based on policy."""
        match route.topic:
            case Topic.SYSTEM:
                return None
            case Topic.DIGITAL | Topic.ANALOG:
                if not route.segments:
                    action = None
                elif len(route.segments) == 1:
                    action = "write"
                else:
                    action = route.segments[1].strip().lower() or None
            case Topic.CONSOLE:
                action = "in" if route.identifier == "in" else None
            case _:
                action = route.identifier

        if action and not self.is_topic_action_allowed(route.topic, action):
            return action
        return None

    def _is_frame_allowed_pre_sync(self, command_id: int) -> bool:
        return (
            self.state.is_synchronized or command_id in STATUS_VALUES or command_id in _PRE_SYNC_ALLOWED_COMMANDS
        )

    # --- MQTT Handlers (Consolidated with match/case) ---

    async def _handle_system_topic(self, route: TopicRoute, inbound: Message) -> bool:
        match route.identifier:
            case "bridge":
                return await self._handle_bridge_topic(route, inbound)
            case _:
                if self._container:
                    from . import SystemComponent

                    system = self._container.get(SystemComponent)
                    return await system.handle_mqtt(route.identifier, list(route.remainder), inbound)
        return False

    async def _handle_bridge_topic(self, route: TopicRoute, inbound: Message) -> bool:
        segments = list(route.remainder)
        if not segments:
            return False
        match segments:
            case ["handshake", "get"]:
                await self.publish_bridge_snapshot("handshake", inbound)
                return True
            case [("summary" | "state"), "get"]:
                await self.publish_bridge_snapshot("summary", inbound)
                return True
            case _:
                return False

    @staticmethod
    def _payload_bytes(payload: Any) -> bytes:
        if payload is None:
            return b""
        if isinstance(payload, (bytes, bytearray)):
            return bytes(payload)
        if isinstance(payload, memoryview):
            return payload.tobytes()
        return str(payload).encode("utf-8")
