"""Command dispatch logic for the MCU Bridge service."""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from typing import TYPE_CHECKING, Any

from mcubridge.protocol.protocol import (
    Command,
    Status,
    response_to_request,
)
from mcubridge.protocol.topics import Topic, TopicRoute
from mcubridge.state.context import RuntimeState

import structlog

if TYPE_CHECKING:
    from aiomqtt import Message

logger = structlog.get_logger("mcubridge.dispatcher")

McuHandler = Callable[[int, bytes], Awaitable[bool | None]]
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
        reject_topic_action: Callable[["Message", Topic, str], Awaitable[None]],
        publish_bridge_snapshot: Callable[[str, Any | None], Awaitable[None]],
        on_frame_received: Callable[[int, int, bytes], None] | None = None,
    ) -> None:
        self.mcu_registry = mcu_registry
        self.mqtt_handlers: dict[Topic, MqttHandler] = {}
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
                self.mqtt_handlers[topic] = handler

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

            # [SIL-2] Direct Enum resolution for high-signal logging
            try:
                command_name = Command(command_id).name
            except ValueError:
                command_name = f"0x{command_id:02X}"

            handled_successfully = False

            if handler:
                logger.debug(
                    "MCU > %s (seq=%d) [%d bytes]",
                    command_name,
                    sequence_id,
                    len(payload),
                )
                handled_successfully = (
                    await handler(sequence_id, payload)
                ) is not False

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
        inbound: "Message",
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
            handler = self.mqtt_handlers.get(route.topic)
            if handler:
                if not await handler(route, inbound):
                    logger.debug("Unhandled MQTT topic %s", inbound_topic)
            else:
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
                return (
                    "write"
                    if len(route.segments) == 1
                    else (route.segments[1].lower() or None)
                )
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

    async def _handle_system_topic(self, route: TopicRoute, inbound: "Message") -> bool:
        match route.identifier:
            case "bridge":
                return await self._handle_bridge_topic(route, inbound)
            case _:
                if self.system:
                    return await self.system.handle_mqtt(route, inbound)
        return False

    async def _handle_bridge_topic(self, route: TopicRoute, inbound: "Message") -> bool:
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
