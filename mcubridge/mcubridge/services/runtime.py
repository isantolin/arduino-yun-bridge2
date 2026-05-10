from __future__ import annotations

import asyncio
import structlog
from collections.abc import Coroutine, Callable
from typing import Any

import msgspec
from aiomqtt.message import Message

from ..config.const import MQTT_EXPIRY_SHELL, TOPIC_FORBIDDEN_REASON
from ..config.settings import RuntimeConfig
from ..protocol.protocol import Command, Status, response_to_request
from ..protocol.structures import AckPacket, QueuedPublish
from ..protocol.topics import Topic, parse_topic, topic_path, TopicRoute
from ..state.context import RuntimeState

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
from .handshake import (
    SendFrameCallable,
    SerialHandshakeManager,
    SerialTimingWindow,
    derive_serial_timing,
)
from .serial_flow import SerialFlowController

McuHandler = Callable[[int, bytes], Coroutine[Any, Any, bool | None]]

logger = structlog.get_logger("mcubridge.service")

_PRE_SYNC_ALLOWED_COMMANDS = {
    Command.CMD_LINK_SYNC_RESP.value,
    Command.CMD_LINK_RESET_RESP.value,
}

STATUS_VALUES = {s.value for s in Status}


class BridgeService:
    """Service façade orchestrating MCU and MQTT interactions. [SIL-2]"""

    def __init__(
        self, config: RuntimeConfig, state: RuntimeState, mqtt_transport: Any
    ) -> None:
        self.config = config
        self.state = state
        self.mqtt_flow = mqtt_transport
        self._serial_timing: SerialTimingWindow = derive_serial_timing(config)
        self._task_group: asyncio.TaskGroup | None = None

        self.serial_flow = SerialFlowController(
            ack_timeout=self._serial_timing.ack_timeout_seconds,
            response_timeout=self._serial_timing.response_timeout_seconds,
            max_attempts=self._serial_timing.retry_limit,
            logger=logger,
        )
        self.serial_flow.set_pipeline_observer(state.record_serial_pipeline_event)

        # [SIL-2] Explicit component instantiation (Zero-Wrapper)
        self.console = ConsoleComponent(config, state, self.serial_flow, self.mqtt_flow)
        self.datastore = DatastoreComponent(
            config, state, self.serial_flow, self.mqtt_flow
        )
        self.file = FileComponent(config, state, self.serial_flow, self.mqtt_flow)
        self.mailbox = MailboxComponent(config, state, self.serial_flow, self.mqtt_flow)
        self.pin = PinComponent(config, state, self.serial_flow, self.mqtt_flow)
        self.process = ProcessComponent(config, state, self.serial_flow, self.mqtt_flow)
        self.spi = SpiComponent(config, state, self.serial_flow, self.mqtt_flow)
        self.system = SystemComponent(config, state, self.serial_flow, self.mqtt_flow)

        self.handshake_manager = SerialHandshakeManager(
            config=config,
            state=state,
            serial_timing=self._serial_timing,
            send_frame=self.serial_flow.send,
            enqueue_mqtt=mqtt_transport.enqueue_mqtt,
            acknowledge_frame=self.serial_flow.acknowledge,
            logger_=logger,
        )

        state.serial_ack_timeout_ms = self._serial_timing.ack_timeout_ms
        state.serial_response_timeout_ms = self._serial_timing.response_timeout_ms
        state.serial_retry_limit = self._serial_timing.retry_limit

        self.mcu_registry: dict[int, McuHandler] = {
            Command.CMD_XOFF.value: self.console.handle_xoff,
            Command.CMD_XON.value: self.console.handle_xon,
            Command.CMD_CONSOLE_WRITE.value: self.console.handle_write,
            Command.CMD_DATASTORE_PUT.value: self.datastore.handle_put,
            Command.CMD_DATASTORE_GET.value: self.datastore.handle_get_request,
            Command.CMD_MAILBOX_PUSH.value: self.mailbox.handle_push,
            Command.CMD_MAILBOX_AVAILABLE.value: self.mailbox.handle_available,
            Command.CMD_MAILBOX_READ.value: self.mailbox.handle_read,
            Command.CMD_MAILBOX_PROCESSED.value: self.mailbox.handle_processed,
            Command.CMD_FILE_WRITE.value: self.file.handle_write,
            Command.CMD_FILE_READ.value: self.file.handle_read,
            Command.CMD_FILE_REMOVE.value: self.file.handle_remove,
            Command.CMD_FILE_READ_RESP.value: self.file.handle_read_response,
            Command.CMD_PROCESS_RUN_ASYNC.value: self.process.handle_run_async,
            Command.CMD_PROCESS_POLL.value: self.process.handle_poll,
            Command.CMD_DIGITAL_READ_RESP.value: self.pin.handle_digital_read_resp,
            Command.CMD_ANALOG_READ_RESP.value: self.pin.handle_analog_read_resp,
            Command.CMD_DIGITAL_READ.value: self.pin.handle_mcu_digital_read,
            Command.CMD_ANALOG_READ.value: self.pin.handle_mcu_analog_read,
            Command.CMD_SPI_TRANSFER_RESP.value: self.spi.handle_transfer_resp,
            Command.CMD_LINK_SYNC_RESP.value: self.handshake_manager.handle_link_sync_resp,
            Command.CMD_LINK_RESET_RESP.value: self.handshake_manager.handle_link_reset_resp,
            Command.CMD_GET_CAPABILITIES_RESP.value: self.handshake_manager.handle_capabilities_resp,
            Command.CMD_PROCESS_KILL.value: self.process.handle_kill,
            Status.ACK.value: self._handle_ack,
        }
        for status in Status:
            if status == Status.ACK:
                continue
            self.mcu_registry[status.value] = (
                lambda s: lambda seq, p: self.handle_status(seq, s, p)
            )(status)

    async def dispatch_mcu_frame(
        self, command_id: int, sequence_id: int, payload: bytes
    ) -> None:
        """
        Route an incoming frame from the MCU to the appropriate registered handler.
        """
        now = asyncio.get_running_loop().time()
        try:
            if True:  # [SIL-2] observer is always configured
                self.serial_flow.on_frame_received(command_id, sequence_id, payload)

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
                await self.serial_flow.send(Status.NOT_IMPLEMENTED.value, b"")

            if handled_successfully and command_id not in STATUS_VALUES:
                await self.serial_flow.acknowledge(command_id, sequence_id)
        finally:
            latency_ms = (asyncio.get_running_loop().time() - now) * 1000.0
            self.state.metrics.serial_latency_ms.observe(latency_ms)

    async def dispatch_mqtt_message(
        self,
        inbound: "Message",
        route: TopicRoute,
    ) -> None:
        start = asyncio.get_running_loop().time()
        try:
            inbound_topic = str(inbound.topic)
            if not route.segments:
                return

            # 1. Policy Guard
            if action := self._get_topic_action(route):
                if not self._is_topic_action_allowed(route.topic, action):
                    await self._reject_topic_action(inbound, route.topic, action)
                    return

            # 2. Synchronization Guard
            if route.topic != Topic.SYSTEM:
                try:
                    async with asyncio.timeout(30.0):
                        await self.state.link_sync_event.wait()
                except asyncio.TimeoutError:
                    logger.warning("MQTT > Link sync timeout for %s", inbound_topic)
                    return

            # 3. Declarative Dispatch (O(1) Pattern Matching)
            # Eradicates the dynamic 'mqtt_handlers' registry.
            match route.topic:
                case Topic.CONSOLE:
                    if self.console:
                        await self.console.handle_mqtt(route, inbound)
                case Topic.DATASTORE:
                    if self.datastore:
                        await self.datastore.handle_mqtt(route, inbound)
                case Topic.MAILBOX:
                    if self.mailbox:
                        await self.mailbox.handle_mqtt(route, inbound)
                case Topic.FILE:
                    if self.file:
                        await self.file.handle_mqtt(route, inbound)
                case Topic.SHELL:
                    if self.process:
                        await self.process.handle_mqtt(route, inbound)
                case Topic.DIGITAL:
                    if self.pin:
                        await self.pin.handle_mqtt(route, inbound)
                case Topic.ANALOG:
                    if self.pin:
                        await self.pin.handle_mqtt(route, inbound)
                case Topic.SPI:
                    if self.spi:
                        await self.spi.handle_mqtt(route, inbound)
                case Topic.SYSTEM:
                    await self._handle_system_topic(route, inbound)
                case _:
                    logger.debug("Unhandled MQTT topic %s", inbound_topic)
        finally:
            latency_ms = (asyncio.get_running_loop().time() - start) * 1000.0
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
                await self._publish_bridge_snapshot("handshake", inbound)
                return True
            case [("summary" | "state"), "get"]:
                await self._publish_bridge_snapshot("summary", inbound)
                return True
            case _:
                return False
        return False

    async def __aenter__(self) -> BridgeService:
        self._task_group = asyncio.TaskGroup()
        await self._task_group.__aenter__()
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc_val: BaseException | None,
        exc_tb: Any,
    ) -> None:
        if self._task_group:
            await self._task_group.__aexit__(exc_type, exc_val, exc_tb)

    def register_serial_sender(self, sender: SendFrameCallable) -> None:
        """Allow the serial transport to provide its send coroutine."""
        self.serial_flow.set_sender(sender)

    async def schedule_background(
        self,
        coroutine: Coroutine[Any, Any, None],
        *,
        name: str | None = None,
    ) -> asyncio.Task[Any]:
        """Schedule *coroutine* under the supervisor."""
        if not self._task_group:
            raise RuntimeError("BridgeService context not entered")

        return self._task_group.create_task(coroutine, name=name)

    async def on_serial_connected(self) -> None:
        """Initiate protocol handshake and flush backlogs after reconnect."""
        self.state.mark_transport_connected()

        # [SIL-2] Protocol Synchronization: Force handshake immediately.
        try:
            await self.handshake_manager.synchronize()
        except (OSError, ValueError, RuntimeError) as e:
            logger.exception("Failed to synchronize link after reconnect: %s", e)

        # [SIL-2] Boundary Guard: Do not proceed if synchronization failed.
        if not self.state.is_synchronized:
            logger.warning(
                "Link synchronization failed; aborting post-connection initialization"
            )
            self.handshake_manager.raise_if_handshake_fatal()
            return

        try:
            version_ok = await self.system.request_mcu_version()
            if not version_ok:
                logger.warning("Failed to dispatch MCU version request after reconnect")
        except (OSError, ValueError, RuntimeError) as e:
            logger.exception("Failed to request MCU version after reconnect: %s", e)

        try:
            await self.console.flush_queue()
        except (OSError, ValueError, RuntimeError) as e:
            logger.exception("Failed to flush console backlog after reconnect: %s", e)

    async def on_serial_disconnected(self) -> None:
        """Reset transient MCU tracking when the serial link drops."""

        self.state.mark_transport_disconnected()

        pending_digital = len(self.state.pending_digital_reads)
        pending_analog = len(self.state.pending_analog_reads)

        total_pending = pending_digital + pending_analog
        if total_pending:
            logger.warning(
                "Serial link lost; clearing %d pending request(s) (digital=%d analog=%d)",
                total_pending,
                pending_digital,
                pending_analog,
            )

        self.state.pending_digital_reads.clear()
        self.state.pending_analog_reads.clear()

        # Ensure we do not keep the console in a paused state between links.
        self.console.on_serial_disconnected()
        await self.serial_flow.reset()
        self.handshake_manager.clear_handshake_expectations()

    async def handle_mcu_frame(
        self, command_id: int, sequence_id: int, payload: bytes
    ) -> None:
        """Entry point invoked by the serial transport for each MCU frame."""
        await self.dispatch_mcu_frame(command_id, sequence_id, payload)

    async def handle_mqtt_message(self, inbound: Message) -> None:
        """Entry point invoked by the MQTT transport for each inbound message."""
        route = parse_topic(self.state.mqtt_topic_prefix, str(inbound.topic))
        if route:
            await self.dispatch_mqtt_message(inbound, route)

    # --- MCU command handlers ---

    async def _handle_ack(self, seq_id: int, payload: bytes) -> None:
        if len(payload) >= 2:
            try:
                # [SIL-2] Use direct msgspec.msgpack.decode (Zero Wrapper)
                packet = msgspec.msgpack.decode(payload, type=AckPacket)
                command_id = packet.command_id
                logger.debug("MCU > ACK received for 0x%02X", command_id)
            except (msgspec.ValidationError, ValueError) as exc:
                logger.warning("MCU > Malformed ACK payload: %s", exc)
        else:
            logger.debug("MCU > ACK received")

    async def handle_status(self, seq_id: int, status: Status, payload: bytes) -> None:
        # [SIL-2] Direct metrics recording (No Wrapper)
        self.state.mcu_status_counts[status.name] = (
            self.state.mcu_status_counts.get(status.name, 0) + 1
        )
        self.state.metrics.mcu_status_counts.labels(status=status.name).inc()

        # [SIL-2] Improved status reporting with descriptive names from protocol
        desc = status.description
        text = payload.decode("utf-8", errors="ignore") if payload else ""

        log_method = (
            logger.warning if status not in {Status.OK, Status.ACK} else logger.debug
        )
        if text:
            log_method("MCU > %s (seq=%d): %s (%s)", status.name, seq_id, desc, text)
        else:
            log_method("MCU > %s (seq=%d): %s", status.name, seq_id, desc)

        # [SIL-2] Use direct msgspec.msgpack.encode (Zero Wrapper)
        report = msgspec.msgpack.encode(
            {
                "status": status.value,
                "name": status.name,
                "description": desc,
                "message": text,
            }
        )
        status_topic = topic_path(
            self.state.mqtt_topic_prefix,
            Topic.SYSTEM,
            Topic.STATUS,
        )
        properties: list[tuple[str, str]] = [
            ("bridge-status", status.name),
            ("bridge-status-description", desc),
        ]
        if text:
            properties.append(("bridge-status-message", text))
        await self.mqtt_flow.enqueue_mqtt(
            QueuedPublish(
                topic_name=status_topic,
                payload=report,
                content_type="application/msgpack",
                message_expiry_interval=MQTT_EXPIRY_SHELL,
                user_properties=tuple(properties),
            )
        )

    async def _publish_bridge_snapshot(
        self,
        flavor: str,
        inbound: Message | None,
    ) -> None:
        if flavor == "handshake":
            snapshot = self.state.build_handshake_snapshot()
            topic_segments = ("bridge", "handshake", "value")
        else:
            snapshot = self.state.build_bridge_snapshot()
            topic_segments = ("bridge", "summary", "value")
        topic = topic_path(
            self.state.mqtt_topic_prefix,
            Topic.SYSTEM,
            *topic_segments,
        )
        # [SIL-2] Use direct msgspec.msgpack.encode (Zero Wrapper)
        await self.mqtt_flow.enqueue_mqtt(
            QueuedPublish(
                topic_name=topic,
                payload=msgspec.msgpack.encode(snapshot),
                content_type="application/msgpack",
                message_expiry_interval=MQTT_EXPIRY_SHELL,
                user_properties=(("bridge-snapshot", flavor),),
            ),
            reply_context=inbound,
        )

    def _is_topic_action_allowed(
        self,
        topic_type: Topic | str,
        action: str,
    ) -> bool:
        if not action:
            return True
        topic_val = topic_type.value if isinstance(topic_type, Topic) else topic_type
        if self.state.topic_authorization:
            return self.state.topic_authorization.allows(topic_val, action)
        return False

    async def _reject_topic_action(
        self,
        inbound: Message,
        topic_type: Topic | str,
        action: str,
    ) -> None:
        topic_value = topic_type.value if isinstance(topic_type, Topic) else topic_type
        logger.warning(
            "Blocked MQTT action topic=%s action=%s (message topic=%s)",
            topic_value,
            action or "<missing>",
            str(inbound.topic),
        )
        # [SIL-2] Use direct msgspec.msgpack.encode (Zero Wrapper)
        payload = msgspec.msgpack.encode(
            {
                "status": "forbidden",
                "topic": topic_value,
                "action": action,
            }
        )
        status_topic = topic_path(
            self.state.mqtt_topic_prefix,
            Topic.SYSTEM,
            Topic.STATUS,
        )
        await self.mqtt_flow.enqueue_mqtt(
            QueuedPublish(
                topic_name=status_topic,
                payload=payload,
                content_type="application/msgpack",
                message_expiry_interval=MQTT_EXPIRY_SHELL,
                user_properties=(("bridge-error", TOPIC_FORBIDDEN_REASON),),
            ),
            reply_context=inbound,
        )
