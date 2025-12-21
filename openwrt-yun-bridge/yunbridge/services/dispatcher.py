"""Command dispatch logic for the Yun Bridge service."""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any, Callable, Awaitable

from yunbridge.rpc.protocol import (
    RESPONSE_OFFSET,
    Command,
    Status,
)
from yunbridge.protocol.topics import Topic, TopicRoute
from .routers import MCUHandlerRegistry, MQTTRouter

if TYPE_CHECKING:
    from aiomqtt.message import Message as MQTTMessage
    from .components import (
        ConsoleComponent,
        DatastoreComponent,
        FileComponent,
        MailboxComponent,
        PinComponent,
        ProcessComponent,
        ShellComponent,
        SystemComponent,
    )

logger = logging.getLogger("yunbridge.dispatcher")

STATUS_VALUES = {status.value for status in Status}
_PRE_SYNC_ALLOWED_COMMANDS = {
    Command.CMD_LINK_SYNC_RESP.value,
    Command.CMD_LINK_RESET_RESP.value,
}
_STATUS_PAYLOAD_WINDOW = 126  # max(0, _MAX_PAYLOAD_BYTES - 2)


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
        send_frame: Callable[[int, bytes], Awaitable[bool]],
        acknowledge_frame: Callable[..., Awaitable[None]],
        is_link_synchronized: Callable[[], bool],
        is_topic_action_allowed: Callable[[Topic | str, str], bool],
        reject_topic_action: Callable[[MQTTMessage, Topic | str, str], Awaitable[None]],
        publish_bridge_snapshot: Callable[[str, MQTTMessage | None], Awaitable[None]],
    ) -> None:
        self.mcu_registry = mcu_registry
        self.mqtt_router = mqtt_router
        self.send_frame = send_frame
        self.acknowledge_frame = acknowledge_frame
        self.is_link_synchronized = is_link_synchronized
        self.is_topic_action_allowed = is_topic_action_allowed
        self.reject_topic_action = reject_topic_action
        self.publish_bridge_snapshot = publish_bridge_snapshot

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
        self.mcu_registry.register(Command.CMD_PROCESS_RUN_ASYNC.value, process.handle_run_async)
        self.mcu_registry.register(Command.CMD_PROCESS_POLL.value, process.handle_poll)
        # CMD_PROCESS_KILL is handled via register_system_handlers or manually if needed

        # Shell (MQTT only)
        self.mqtt_router.register(Topic.SHELL, self._handle_shell_topic)

        # Pin (GPIO)
        self.mcu_registry.register(Command.CMD_DIGITAL_READ_RESP.value, pin.handle_digital_read_resp)
        self.mcu_registry.register(Command.CMD_ANALOG_READ_RESP.value, pin.handle_analog_read_resp)
        self.mcu_registry.register(Command.CMD_DIGITAL_READ.value, lambda p: pin.handle_unexpected_mcu_request(Command.CMD_DIGITAL_READ, p))
        self.mcu_registry.register(Command.CMD_ANALOG_READ.value, lambda p: pin.handle_unexpected_mcu_request(Command.CMD_ANALOG_READ, p))
        self.mqtt_router.register(Topic.DIGITAL, self._handle_pin_topic)
        self.mqtt_router.register(Topic.ANALOG, self._handle_pin_topic)

        # System
        self.mcu_registry.register(Command.CMD_GET_FREE_MEMORY_RESP.value, system.handle_get_free_memory_resp)
        self.mcu_registry.register(Command.CMD_GET_VERSION_RESP.value, system.handle_get_version_resp)
        self.mcu_registry.register(Command.CMD_GET_TX_DEBUG_SNAPSHOT_RESP.value, system.handle_get_tx_debug_snapshot_resp)
        self.mqtt_router.register(Topic.SYSTEM, self._handle_system_topic)

    def register_system_handlers(
        self,
        handle_link_sync_resp: Callable[[bytes], Awaitable[bool]],
        handle_link_reset_resp: Callable[[bytes], Awaitable[bool]],
        handle_ack: Callable[[bytes], Awaitable[None]],
        status_handler_factory: Callable[[Status], Callable[[bytes], Awaitable[None]]],
        handle_process_kill: Callable[[bytes], Awaitable[bool | None]],
    ) -> None:
        self.mcu_registry.register(Command.CMD_LINK_SYNC_RESP.value, handle_link_sync_resp)
        self.mcu_registry.register(Command.CMD_LINK_RESET_RESP.value, handle_link_reset_resp)
        self.mcu_registry.register(Command.CMD_PROCESS_KILL.value, handle_process_kill)

        self.mcu_registry.register(Status.ACK.value, handle_ack)
        for status in Status:
            if status == Status.ACK:
                continue
            self.mcu_registry.register(status.value, status_handler_factory(status))

    async def dispatch_mcu_frame(self, command_id: int, payload: bytes) -> None:
        if not self._is_frame_allowed_pre_sync(command_id):
            logger.warning(
                "Rejecting MCU frame 0x%02X before link synchronisation",
                command_id,
            )
            if command_id < RESPONSE_OFFSET:
                await self.acknowledge_frame(
                    command_id,
                    status=Status.MALFORMED,
                    extra=payload[:_STATUS_PAYLOAD_WINDOW],
                )
            return

        handler = self.mcu_registry.get(command_id)
        command_name: str | None = None
        try:
            command_name = Command(command_id).name
        except ValueError:
            try:
                command_name = Status(command_id).name
            except ValueError:
                command_name = f"UNKNOWN_CMD_ID(0x{command_id:02X})"

        handled_successfully = False

        if handler:
            logger.debug("MCU > %s payload=%s", command_name, payload.hex())
            result = await handler(payload)
            handled_successfully = result is not False
        elif command_id < RESPONSE_OFFSET:
            logger.warning("Unhandled MCU command %s", command_name)
            await self.send_frame(Status.NOT_IMPLEMENTED.value, b"")
        else:
            logger.debug("Ignoring MCU response %s", command_name)

        if handled_successfully and self._should_acknowledge_mcu_frame(command_id):
            await self.acknowledge_frame(command_id)

    async def dispatch_mqtt_message(
        self,
        inbound: MQTTMessage,
        parse_topic_func: Callable[[str], TopicRoute | None],
    ) -> None:
        inbound_topic = str(inbound.topic)
        # We need parse_topic_func passed in or imported
        route = parse_topic_func(inbound_topic)
        if route is None:
            logger.debug(
                "Ignoring MQTT message with unexpected prefix: %s",
                inbound_topic,
            )
            return

        if not route.segments:
            logger.debug("MQTT topic missing identifier: %s", inbound_topic)
            return

        try:
            handled = await self.mqtt_router.dispatch(route, inbound)
        except Exception:
            logger.exception("Error processing MQTT topic: %s", inbound_topic)
            return

        if not handled:
            logger.debug("Unhandled MQTT topic %s", inbound_topic)

    def _should_acknowledge_mcu_frame(self, command_id: int) -> bool:
        return command_id not in STATUS_VALUES

    def _is_frame_allowed_pre_sync(self, command_id: int) -> bool:
        if self.is_link_synchronized():
            return True
        if command_id in STATUS_VALUES:
            return True
        return command_id in _PRE_SYNC_ALLOWED_COMMANDS

    # --- MQTT Handlers ---

    async def _handle_file_topic(self, route: TopicRoute, inbound: MQTTMessage) -> bool:
        if len(route.segments) < 2:
            return False
        identifier = route.identifier
        if not self.is_topic_action_allowed(route.topic, identifier):
            await self.reject_topic_action(inbound, route.topic, identifier)
            return True
        payload = self._payload_bytes(inbound.payload)
        if self.file:
            await self.file.handle_mqtt(identifier, list(route.remainder), payload, inbound)
        return True

    async def _handle_console_topic(self, route: TopicRoute, inbound: MQTTMessage) -> bool:
        if route.identifier != "in":
            return False
        action = "input"
        if not self.is_topic_action_allowed(Topic.CONSOLE, action):
            await self.reject_topic_action(inbound, Topic.CONSOLE, action)
            return True
        payload = self._payload_bytes(inbound.payload)
        if self.console:
            await self.console.handle_mqtt_input(payload, inbound)
        return True

    async def _handle_datastore_topic(self, route: TopicRoute, inbound: MQTTMessage) -> bool:
        identifier = route.identifier
        if not identifier:
            return False
        if not self.is_topic_action_allowed(route.topic, identifier):
            await self.reject_topic_action(inbound, route.topic, identifier)
            return True
        payload = self._payload_bytes(inbound.payload)
        payload_str = payload.decode("utf-8", errors="ignore")
        if self.datastore:
            await self.datastore.handle_mqtt(identifier, list(route.remainder), payload, payload_str, inbound)
        return True

    async def _handle_mailbox_topic(self, route: TopicRoute, inbound: MQTTMessage) -> bool:
        identifier = route.identifier
        if identifier and not self.is_topic_action_allowed(route.topic, identifier):
            await self.reject_topic_action(inbound, route.topic, identifier)
            return True
        payload = self._payload_bytes(inbound.payload)
        if self.mailbox:
            await self.mailbox.handle_mqtt(identifier, payload, inbound)
        return True

    async def _handle_shell_topic(self, route: TopicRoute, inbound: MQTTMessage) -> bool:
        identifier = route.identifier
        if identifier and not self.is_topic_action_allowed(route.topic, identifier):
            await self.reject_topic_action(inbound, route.topic, identifier)
            return True
        payload = self._payload_bytes(inbound.payload)
        if self.shell:
            await self.shell.handle_mqtt(route.raw.split("/"), payload, inbound)
        return True

    async def _handle_pin_topic(self, route: TopicRoute, inbound: MQTTMessage) -> bool:
        payload = self._payload_bytes(inbound.payload)
        payload_str = payload.decode("utf-8", errors="ignore")
        parts = route.raw.split("/")
        action = self._pin_action_from_parts(parts)
        if action and not self.is_topic_action_allowed(route.topic, action):
            await self.reject_topic_action(inbound, route.topic, action)
            return True
        if self.pin:
            await self.pin.handle_mqtt(route.topic, parts, payload_str, inbound)
        return True

    async def _handle_system_topic(self, route: TopicRoute, inbound: MQTTMessage) -> bool:
        if route.identifier == "bridge":
            bridge_handled = await self._handle_bridge_topic(route, inbound)
            if bridge_handled:
                return True
        if self.system:
            handled = await self.system.handle_mqtt(route.identifier, list(route.remainder), inbound)
            if not handled:
                logger.debug("Unhandled MQTT system topic %s", route.raw)
            return handled
        return False

    async def _handle_bridge_topic(self, route: TopicRoute, inbound: MQTTMessage) -> bool:
        segments = list(route.remainder)
        if not segments:
            return False
        category = segments[0]
        action = segments[1] if len(segments) > 1 else ""
        if category == "handshake" and action == "get":
            await self.publish_bridge_snapshot("handshake", inbound)
            return True
        if category in {"summary", "state"} and action == "get":
            await self.publish_bridge_snapshot("summary", inbound)
            return True
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
        if isinstance(payload, bytes):
            return payload
        if isinstance(payload, bytearray):
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
