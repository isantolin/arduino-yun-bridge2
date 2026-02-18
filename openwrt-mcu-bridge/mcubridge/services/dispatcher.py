"""Command dispatch logic for the MCU Bridge service."""

from __future__ import annotations

import logging
from functools import partial
from typing import TYPE_CHECKING, Any, Awaitable, Callable

from mcubridge.protocol.contracts import response_to_request
from mcubridge.protocol.protocol import (
    MAX_PAYLOAD_SIZE,
    Command,
    Status,
)
from mcubridge.protocol.topics import Topic, TopicRoute
from mcubridge.state.context import resolve_command_id, RuntimeState

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
_STATUS_PAYLOAD_WINDOW = max(0, int(MAX_PAYLOAD_SIZE) - 2)


class BridgeDispatcher:
    """Decoupled dispatch logic for MCU frames and MQTT messages.

    This class is responsible for routing incoming MCU frames (from the Arduino)
    and MQTT messages (from the network) to the appropriate handling components
    or system-level functions. It acts as a central hub for command processing,
    ensuring that each command/message is directed to the correct handler based
    on its ID (for MCU frames) or topic (for MQTT messages).

    It registers various service components and system handlers to
    manage the interaction between the Linux side and the MCU.
    """

    def __init__(
        self,
        mcu_registry: MCUHandlerRegistry,
        mqtt_router: MQTTRouter,
        state: RuntimeState,
        send_frame: Callable[[int, bytes], Awaitable[bool]],
        acknowledge_frame: Callable[..., Awaitable[None]],
        is_topic_action_allowed: Callable[[Topic | str, str], bool],
        reject_topic_action: Callable[[Message, Topic | str, str], Awaitable[None]],
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

        # Components (populated via register_components)
        self.console: ConsoleComponent | None = None
        self.datastore: DatastoreComponent | None = None
        self.file: FileComponent | None = None
        self.mailbox: MailboxComponent | None = None
        self.pin: PinComponent | None = None
        self.process: ProcessComponent | None = None
        self.shell: ShellComponent | None = None
        self.system: SystemComponent | None = None

    def register_components(
        self,
        console: ConsoleComponent,
        datastore: DatastoreComponent,
        file: FileComponent,
        mailbox: MailboxComponent,
        pin: PinComponent,
        process: ProcessComponent,
        shell: ShellComponent,
        system: SystemComponent,
    ) -> None:
        """Register all component handlers with the registries."""
        self.console = console
        self.datastore = datastore
        self.file = file
        self.mailbox = mailbox
        self.pin = pin
        self.process = process
        self.shell = shell
        self.system = system

        # Console
        self.mcu_registry.register(Command.CMD_XOFF.value, console.handle_xoff)
        self.mcu_registry.register(Command.CMD_XON.value, console.handle_xon)
        self.mcu_registry.register(Command.CMD_CONSOLE_WRITE.value, console.handle_write)
        self.mqtt_router.register(Topic.CONSOLE, self._handle_console_topic)

        # Datastore
        self.mcu_registry.register(Command.CMD_DATASTORE_PUT.value, datastore.handle_put)
        self.mcu_registry.register(Command.CMD_DATASTORE_GET.value, datastore.handle_get_request)
        self.mqtt_router.register(Topic.DATASTORE, self._handle_datastore_topic)

        # Mailbox
        self.mcu_registry.register(Command.CMD_MAILBOX_PUSH.value, mailbox.handle_push)
        self.mcu_registry.register(Command.CMD_MAILBOX_AVAILABLE.value, mailbox.handle_available)
        self.mcu_registry.register(Command.CMD_MAILBOX_READ.value, mailbox.handle_read)
        self.mcu_registry.register(Command.CMD_MAILBOX_PROCESSED.value, mailbox.handle_processed)
        self.mqtt_router.register(Topic.MAILBOX, self._handle_mailbox_topic)

        # File
        self.mcu_registry.register(Command.CMD_FILE_WRITE.value, file.handle_write)
        self.mcu_registry.register(Command.CMD_FILE_READ.value, file.handle_read)
        self.mcu_registry.register(Command.CMD_FILE_REMOVE.value, file.handle_remove)
        self.mqtt_router.register(Topic.FILE, self._handle_file_topic)

        # Process
        self.mcu_registry.register(Command.CMD_PROCESS_RUN.value, process.handle_run)
        self.mcu_registry.register(
            Command.CMD_PROCESS_RUN_ASYNC.value,
            process.handle_run_async,
        )
        self.mcu_registry.register(Command.CMD_PROCESS_POLL.value, process.handle_poll)
        # CMD_PROCESS_KILL is handled via register_system_handlers or manually if needed

        # Shell (MQTT only)
        self.mqtt_router.register(Topic.SHELL, self._handle_shell_topic)

        # Pin (GPIO)
        self.mcu_registry.register(
            Command.CMD_DIGITAL_READ_RESP.value,
            pin.handle_digital_read_resp,
        )
        self.mcu_registry.register(
            Command.CMD_ANALOG_READ_RESP.value,
            pin.handle_analog_read_resp,
        )
        self.mcu_registry.register(
            Command.CMD_DIGITAL_READ.value,
            partial(self._handle_unexpected_pin_read, Command.CMD_DIGITAL_READ),
        )
        self.mcu_registry.register(
            Command.CMD_ANALOG_READ.value,
            partial(self._handle_unexpected_pin_read, Command.CMD_ANALOG_READ),
        )
        self.mqtt_router.register(Topic.DIGITAL, self._handle_pin_topic)
        self.mqtt_router.register(Topic.ANALOG, self._handle_pin_topic)

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

    async def _handle_unexpected_pin_read(self, command: Command, payload: bytes) -> bool:
        """Route unexpected pin read from MCU to pin component (shared impl)."""
        pin = self.pin
        if pin is None:
            logger.warning("Pin component not registered; dropping unexpected %s", command.name)
            return False
        return await pin.handle_unexpected_mcu_request(command, payload)

    async def dispatch_mcu_frame(self, command_id: int, payload: bytes) -> None:
        """
        Route an incoming frame from the MCU to the appropriate registered handler.

        This method acts as a Firewall/Router. It enforces pre-sync validation
        and wraps handler execution in a safety try/except block to prevent
        service crashes due to component failures.
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
            # IMPORTANT: Do not send any reply frames while not synchronized.
            # Responding (ACK/STATUS) can create a feedback loop that floods the
            # serial link and increases frame corruption / RX overflows.
            return

        # 2. Handler Resolution
        handler = self.mcu_registry.get(command_id)
        command_name = resolve_command_id(command_id)

        # 3. Safe Execution Strategy
        handled_successfully = False

        if handler:
            # [LOGGING] Debug level only to keep production logs clean
            logger.debug("MCU > %s [%d bytes]", command_name, len(payload))

            try:
                # Execute the component handler
                result = await handler(payload)
                handled_successfully = result is not False
            except (OSError, ValueError, TypeError, AttributeError, KeyError, IndexError, RuntimeError) as exc:
                # [RESILIENCE] Catch component crashes so the Dispatcher stays alive.
                logger.critical("Critical: Exception in handler for command %s: %s", command_name, exc, exc_info=True)
                # Optionally send an error status back to MCU if it was a request
                if response_to_request(command_id) is None:
                    await self.send_frame(Status.ERROR.value, b"Internal Error")

        elif response_to_request(command_id) is None:
            logger.warning("Protocol: Unhandled MCU command %s (No handler registered)", command_name)
            self.state.record_unknown_command_id(command_id)
            await self.send_frame(Status.NOT_IMPLEMENTED.value, b"")
        else:
            # It's a response ID but no one was waiting for it (or it arrived late)
            logger.debug("Protocol: Ignoring orphaned MCU response %s", command_name)

        # 4. Auto-Acknowledgement (if applicable)
        if handled_successfully and self._should_acknowledge_mcu_frame(command_id):
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
        except (OSError, ValueError, TypeError, AttributeError, KeyError, IndexError, RuntimeError):
            logger.exception("Error processing MQTT topic: %s", inbound_topic)
            return

        if not handled:
            logger.debug("Unhandled MQTT topic %s", inbound_topic)

    def _should_reject_topic_action(self, route: TopicRoute) -> str | None:
        """Deduce if an MQTT route should be rejected based on policy.
        Returns the action name if it should be checked and is forbidden, else None.
        """
        match route.topic:
            case Topic.SYSTEM:
                return None  # System topics are not subject to TopicAuthorization
            case Topic.DIGITAL | Topic.ANALOG:
                action = self._pin_action_from_parts(route.raw.split("/"))
            case Topic.CONSOLE:
                action = "in" if route.identifier == "in" else None
            case _:
                action = route.identifier

        if action and not self.is_topic_action_allowed(route.topic, action):
            return action
        return None

    def _should_acknowledge_mcu_frame(self, command_id: int) -> bool:
        return command_id not in STATUS_VALUES

    def _is_frame_allowed_pre_sync(self, command_id: int) -> bool:
        if self.state.link_is_synchronized:
            return True
        if command_id in STATUS_VALUES:
            return True
        return command_id in _PRE_SYNC_ALLOWED_COMMANDS

    async def _guard_dispatch(self, route: TopicRoute, inbound: Message) -> bytes | None:
        """Enforces policy. Returns payload if allowed, None if rejected (rejection sent)."""
        if action := self._should_reject_topic_action(route):
            await self.reject_topic_action(inbound, route.topic, action)
            return None
        return self._payload_bytes(inbound.payload)

    # --- MQTT Handlers (Consolidated with match/case) ---

    async def _handle_file_topic(self, route: TopicRoute, inbound: Message) -> bool:
        if len(route.segments) < 2:
            return False
        payload = await self._guard_dispatch(route, inbound)
        if payload is None:
            return True
        if self.file:
            await self.file.handle_mqtt(route.identifier, list(route.remainder), payload, inbound)
        return True

    async def _handle_console_topic(self, route: TopicRoute, inbound: Message) -> bool:
        if route.identifier != "in":
            return False
        payload = await self._guard_dispatch(route, inbound)
        if payload is None:
            return True
        if self.console:
            await self.console.handle_mqtt_input(payload, inbound)
        return True

    async def _handle_datastore_topic(self, route: TopicRoute, inbound: Message) -> bool:
        if not route.identifier:
            return False
        payload = await self._guard_dispatch(route, inbound)
        if payload is None:
            return True
        if self.datastore:
            payload_str = payload.decode("utf-8", errors="ignore")
            await self.datastore.handle_mqtt(route.identifier, list(route.remainder), payload, payload_str, inbound)
        return True

    async def _handle_mailbox_topic(self, route: TopicRoute, inbound: Message) -> bool:
        if not route.identifier:
            return False
        payload = await self._guard_dispatch(route, inbound)
        if payload is None:
            return True
        if self.mailbox:
            await self.mailbox.handle_mqtt(route.identifier, payload, inbound)
        return True

    async def _handle_shell_topic(self, route: TopicRoute, inbound: Message) -> bool:
        if not route.identifier:
            return False
        payload = await self._guard_dispatch(route, inbound)
        if payload is None:
            return True
        if self.shell:
            await self.shell.handle_mqtt(route.raw.split("/"), payload, inbound)
        return True

    async def _handle_pin_topic(self, route: TopicRoute, inbound: Message) -> bool:
        payload = await self._guard_dispatch(route, inbound)
        if payload is None:
            return True
        parts = route.raw.split("/")
        if self.pin:
            payload_str = payload.decode("utf-8", errors="ignore")
            await self.pin.handle_mqtt(route.topic, parts, payload_str, inbound)
        return True

    async def _handle_system_topic(self, route: TopicRoute, inbound: Message) -> bool:
        match route.identifier:
            case "bridge":
                return await self._handle_bridge_topic(route, inbound)
            case _:
                if self.system:
                    return await self.system.handle_mqtt(route.identifier, list(route.remainder), inbound)
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
    def _pin_action_from_parts(parts: list[str]) -> str | None:
        if len(parts) < 3:
            return None
        if len(parts) == 3:
            return "write"
        subtopic = parts[3].strip().lower()
        return subtopic or None

    @staticmethod
    def _payload_bytes(payload: Any) -> bytes:
        if isinstance(payload, (bytes, bytearray)):
            return bytes(payload)
        if isinstance(payload, memoryview):
            return payload.tobytes()
        if payload is None:
            return b""
        if isinstance(payload, str):
            return payload.encode("utf-8")
        if isinstance(payload, (int, float)):
            return str(payload).encode("utf-8")
        raise TypeError(f"Unsupported MQTT payload type: {type(payload)!r}")
