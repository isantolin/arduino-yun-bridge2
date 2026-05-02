"""Command dispatch logic for the MCU Bridge service."""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from typing import TYPE_CHECKING, Any, Optional

from ..protocol.protocol import (
    Command,
    Status,
    response_to_request,
)
from ..protocol.topics import Topic, TopicRoute
from ..state.context import RuntimeState

import structlog

if TYPE_CHECKING:
    from aiomqtt import Message

logger = structlog.get_logger("mcubridge.dispatcher")

McuHandler = Callable[[int, bytes], Awaitable[Optional[bool]]]
MqttHandler = Callable[[TopicRoute, "Message"], Awaitable[bool]]


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
        state: RuntimeState,
        send_frame: Callable[[int, bytes], Awaitable[bool]],
        acknowledge_frame: Callable[[int, int], Awaitable[None]],
        is_topic_action_allowed: Callable[[Topic, str], bool],
        reject_topic_action: Callable[[Message, Topic, str], Awaitable[None]],
        publish_bridge_snapshot: Callable[[str, Message | None], Awaitable[None]],
        on_frame_received: Callable[[int, int, bytes], None] | None = None,
    ) -> None:
        self.mcu_registry = mcu_registry
        self.mqtt_handlers: dict[Topic, list[MqttHandler]] = {}
        self.state = state
        self.send_frame = send_frame
        self.acknowledge_frame = acknowledge_frame
        self.is_topic_action_allowed = is_topic_action_allowed
        self.reject_topic_action = reject_topic_action
        self.publish_bridge_snapshot = publish_bridge_snapshot

        self.on_frame_received_callback = on_frame_received

        # [SIL-2] Direct component references for zero-overhead dispatching.
        self.system: Any | None = None

    def register_components(
        self,
        *,
        console: Any = None,
        datastore: Any = None,
        file: Any = None,
        mailbox: Any = None,
        pin: Any = None,
        process: Any = None,
        spi: Any = None,
        system: Any = None,
    ) -> None:
        """Register all component handlers with the registries."""
        self.system = system

        # MCU Command Dispatch Map (Centralized for auditability)
        # Only register handlers for components that are actually present.
        mcu_map: dict[Command, McuHandler | None] = {
            Command.CMD_XOFF: console.handle_xoff if console else None,
            Command.CMD_XON: console.handle_xon if console else None,
            Command.CMD_CONSOLE_WRITE: console.handle_write if console else None,
            Command.CMD_DATASTORE_PUT: datastore.handle_put if datastore else None,
            Command.CMD_DATASTORE_GET: (
                datastore.handle_get_request if datastore else None
            ),
            Command.CMD_MAILBOX_PUSH: mailbox.handle_push if mailbox else None,
            Command.CMD_MAILBOX_AVAILABLE: (
                mailbox.handle_available if mailbox else None
            ),
            Command.CMD_MAILBOX_READ: mailbox.handle_read if mailbox else None,
            Command.CMD_MAILBOX_PROCESSED: (
                mailbox.handle_processed if mailbox else None
            ),
            Command.CMD_FILE_WRITE: file.handle_write if file else None,
            Command.CMD_FILE_READ: file.handle_read if file else None,
            Command.CMD_FILE_REMOVE: file.handle_remove if file else None,
            Command.CMD_FILE_READ_RESP: file.handle_read_response if file else None,
            Command.CMD_PROCESS_RUN_ASYNC: (
                process.handle_run_async if process else None
            ),
            Command.CMD_PROCESS_POLL: process.handle_poll if process else None,
            Command.CMD_DIGITAL_READ_RESP: (
                pin.handle_digital_read_resp if pin else None
            ),
            Command.CMD_ANALOG_READ_RESP: pin.handle_analog_read_resp if pin else None,
            Command.CMD_DIGITAL_READ: pin.handle_mcu_digital_read if pin else None,
            Command.CMD_ANALOG_READ: pin.handle_mcu_analog_read if pin else None,
            Command.CMD_SPI_TRANSFER_RESP: spi.handle_transfer_resp if spi else None,
            Command.CMD_GET_FREE_MEMORY_RESP: (
                system.handle_get_free_memory_resp if system else None
            ),
            Command.CMD_GET_VERSION_RESP: (
                system.handle_get_version_resp if system else None
            ),
        }
        for cmd, handler in mcu_map.items():
            if handler:
                self.mcu_registry[cmd.value] = handler

        # MQTT Topic Dispatch Map
        mqtt_map = {
            Topic.CONSOLE: console.handle_mqtt if console else None,
            Topic.DATASTORE: datastore.handle_mqtt if datastore else None,
            Topic.MAILBOX: mailbox.handle_mqtt if mailbox else None,
            Topic.FILE: file.handle_mqtt if file else None,
            Topic.SHELL: process.handle_mqtt if process else None,
            Topic.DIGITAL: pin.handle_mqtt if pin else None,
            Topic.ANALOG: pin.handle_mqtt if pin else None,
            Topic.SPI: spi.handle_mqtt if spi else None,
            Topic.SYSTEM: self._handle_system_topic,
        }
        for topic, handler in mqtt_map.items():
            if handler:
                bucket = self.mqtt_handlers.setdefault(topic, [])
                bucket.append(handler)

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
        self.mcu_registry[Command.CMD_GET_CAPABILITIES_RESP.value] = (
            handle_get_capabilities_resp
        )
        self.mcu_registry[Status.ACK.value] = handle_ack
        self.mcu_registry[Command.CMD_PROCESS_KILL.value] = handle_process_kill

        for s_val in STATUS_VALUES:
            # Using literal integer value for registration to avoid duplicate Enum lookup
            self.mcu_registry[s_val] = status_handler_factory(Status(s_val))

    async def dispatch_mcu_frame(
        self, command_id: int, sequence_id: int, payload: bytes
    ) -> None:
        """Route an inbound frame from the MCU to its registered handler."""
        if self.on_frame_received_callback:
            self.on_frame_received_callback(command_id, sequence_id, payload)

        start = asyncio.get_running_loop().time()

        # 1. Synchronization Gate
        if (
            not self.state.is_synchronized
            and command_id not in _PRE_SYNC_ALLOWED_COMMANDS
        ):
            logger.debug(
                "Dropping command 0x%02X (seq=%d) - Link not synchronized.",
                command_id,
                sequence_id,
            )
            return

        # 2. Handler Lookup
        handler = self.mcu_registry.get(command_id)
        if handler is None:
            logger.warning(
                "No handler registered for MCU command 0x%02X (seq=%d)",
                command_id,
                sequence_id,
            )
            self.state.unknown_command_count += 1
            self.state.metrics.unknown_command_count.inc()
            return

        # 3. Execution & Auto-Ack
        try:
            res = await handler(sequence_id, payload)

            # Auto-Ack logic for commands that require it (and weren't rejected)
            if res is not False and response_to_request(command_id) is not None:
                await self.acknowledge_frame(command_id, sequence_id)

        except asyncio.CancelledError:
            raise
        except Exception as exc:
            logger.error(
                "Error executing MCU handler for 0x%02X: %s",
                command_id,
                exc,
                exc_info=True,
            )
        finally:
            latency_ms = (asyncio.get_running_loop().time() - start) * 1000.0
            # [SIL-2] Direct metrics recording (No Wrapper)
            self.state.rpc_latency_stats.record(latency_ms)
            self.state.metrics.rpc_latency_ms.observe(latency_ms)

    async def dispatch_mqtt_message(
        self, inbound: Message, parse_mock: Callable[[str], TopicRoute | None]
    ) -> None:
        """Route an inbound MQTT message to its registered handler."""
        inbound_topic = str(inbound.topic)
        route = parse_mock(inbound_topic)

        start = asyncio.get_running_loop().time()

        if route is None:
            logger.debug("MQTTRouter: No route found for topic %s", inbound_topic)
            return

        # 1. Policy Authorization
        action = self._get_topic_action(route)
        if action:
            if not self.is_topic_action_allowed(route.topic, action):
                logger.warning(
                    "Topic action BLOCKED by policy: %s/%s", route.topic, action
                )
                await self.reject_topic_action(inbound, route.topic, action)
                return

        # 2. Metrics recording
        self.state.mqtt_messages_received += 1
        self.state.metrics.mqtt_messages_received.inc()

        # 3. Router Dispatch
        try:
            dispatched = False
            for handler in self.mqtt_handlers.get(route.topic, []):
                if handler is None:
                    continue
                if await handler(route, inbound):
                    dispatched = True

        except asyncio.CancelledError:
            raise
        except Exception as exc:
            logger.error("Error executing MQTT handler for %s: %s", inbound_topic, exc)
        finally:
            latency_ms = (asyncio.get_running_loop().time() - start) * 1000.0
            # [SIL-2] Direct metrics recording (No Wrapper)
            self.state.rpc_latency_stats.record(latency_ms)
            self.state.metrics.rpc_latency_ms.observe(latency_ms)

    def _get_topic_action(self, route: TopicRoute) -> str | None:
        """Extract logical action (identifier) from route for policy check."""
        # Special cases based on topic structure
        if route.topic in (Topic.DIGITAL, Topic.ANALOG):
            if not route.segments:
                return None
            return route.remainder[0] if len(route.segments) > 1 else "write"
        return route.identifier

    async def _handle_system_topic(self, route: TopicRoute, inbound: Message) -> bool:
        match route.identifier:
            case "bridge":
                return await self._handle_bridge_topic(route, inbound)
            case _:
                if self.system:
                    return await self.system.handle_mqtt(route, inbound)
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


__all__ = ["BridgeDispatcher", "McuHandler", "MqttHandler"]
