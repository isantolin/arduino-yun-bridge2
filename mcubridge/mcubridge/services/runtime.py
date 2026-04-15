from __future__ import annotations

import asyncio
import structlog
import time
from collections.abc import Coroutine
from typing import TYPE_CHECKING, Any

import msgspec
import svcs
from aiomqtt.message import Message

from ..config.const import MQTT_EXPIRY_SHELL, TOPIC_FORBIDDEN_REASON
from ..config.settings import RuntimeConfig
from ..protocol.structures import QueuedPublish
from ..protocol.protocol import Status  # Only Status from rpc.protocol needed
from ..protocol.structures import AckPacket
from ..protocol.topics import Topic, parse_topic, topic_path
from ..router.routers import MQTTRouter
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
from ..protocol.contracts import response_to_request

if TYPE_CHECKING:
    from ..router.routers import McuHandler
    from mcubridge.protocol.topics import TopicRoute

logger = structlog.get_logger("mcubridge.service")
_msgpack_enc = msgspec.msgpack.Encoder()

_PRE_SYNC_ALLOWED_COMMANDS = {
    Status.OK.value,
    Status.ERROR.value,
    Status.CMD_UNKNOWN.value,
    Status.MALFORMED.value,
    Status.OVERFLOW.value,
    Status.CRC_MISMATCH.value,
    Status.TIMEOUT.value,
    Status.NOT_IMPLEMENTED.value,
    # System essentials
    0x02,  # CMD_LINK_SYNC_RESP (Value from generated protocol)
    0x04,  # CMD_LINK_RESET_RESP
}

_STATUS_DESCRIPTIONS: dict[Status, str] = {
    Status.OK: "Operation completed successfully",
    Status.ERROR: "Generic failure",
    Status.CMD_UNKNOWN: "Command not recognized by MCU",
    Status.MALFORMED: "MCU reported malformed payload structure",
    Status.OVERFLOW: "MCU reported buffer overflow (frame exceeded limits)",
    Status.CRC_MISMATCH: "MCU reported CRC32 integrity check failure",
    Status.TIMEOUT: "MCU reported operation timeout",
    Status.NOT_IMPLEMENTED: "Command defined but not supported by MCU",
}


class BridgeService:
    """Service façade orchestrating MCU and MQTT interactions (SIL-2)."""

    def __init__(self, config: RuntimeConfig, state: RuntimeState) -> None:
        self.config = config
        self.state = state
        self._serial_sender: SendFrameCallable | None = None
        self._serial_timing: SerialTimingWindow = derive_serial_timing(config)
        self._task_group: asyncio.TaskGroup | None = None

        self._registry = svcs.Registry()
        _COMPONENTS: tuple[type, ...] = (
            ConsoleComponent,
            DatastoreComponent,
            FileComponent,
            MailboxComponent,
            PinComponent,
            ProcessComponent,
            SpiComponent,
            SystemComponent,
        )
        for comp_cls in _COMPONENTS:
            self._registry.register_factory(  # type: ignore[reportUnknownMemberType]
                comp_cls,
                lambda c=comp_cls: c(config, state, self),  # type: ignore[reportUnknownLambdaType]
            )
        self._container = svcs.Container(self._registry)

        self.handshake_manager = SerialHandshakeManager(
            config=config,
            state=state,
            serial_timing=self._serial_timing,
            send_frame=self.send_frame,
            enqueue_mqtt=self.enqueue_mqtt,
            acknowledge_frame=self.acknowledge_mcu_frame,
            logger_=logger,
        )

        state.serial_ack_timeout_ms = self._serial_timing.ack_timeout_ms
        state.serial_response_timeout_ms = self._serial_timing.response_timeout_ms
        state.serial_retry_limit = self._serial_timing.retry_limit

        self._serial_flow = SerialFlowController(
            ack_timeout=self._serial_timing.ack_timeout_seconds,
            response_timeout=self._serial_timing.response_timeout_seconds,
            max_attempts=self._serial_timing.retry_limit,
            logger=logger,
        )
        self._serial_flow.set_metrics_callback(state.record_serial_flow_event)
        self._serial_flow.set_pipeline_observer(state.record_serial_pipeline_event)

        self._mcu_registry: dict[int, McuHandler] = {}
        self._mqtt_router = MQTTRouter()
        self._register_all_handlers()

    def _register_all_handlers(self) -> None:
        """Centralized registration of all MCU and MQTT handlers (No Wrappers)."""
        console = self._container.get(ConsoleComponent)
        datastore = self._container.get(DatastoreComponent)
        file = self._container.get(FileComponent)
        mailbox = self._container.get(MailboxComponent)
        pin = self._container.get(PinComponent)
        process = self._container.get(ProcessComponent)
        spi = self._container.get(SpiComponent)
        system = self._container.get(SystemComponent)

        # 1. MCU Command Registry (O(1) Jump Table)
        from ..protocol.protocol import Command
        r = self._mcu_registry
        r[Command.CMD_LINK_SYNC_RESP.value] = self.handshake_manager.handle_link_sync_resp
        r[Command.CMD_LINK_RESET_RESP.value] = self.handshake_manager.handle_link_reset_resp
        r[Command.CMD_GET_CAPABILITIES_RESP.value] = (
            self.handshake_manager.handle_capabilities_resp
        )
        r[Command.CMD_GET_VERSION_RESP.value] = system.handle_get_version_resp
        r[Command.CMD_GET_FREE_MEMORY_RESP.value] = system.handle_get_free_memory_resp
        r[Command.CMD_XOFF.value] = console.handle_xoff
        r[Command.CMD_XON.value] = console.handle_xon
        r[Command.CMD_CONSOLE_WRITE.value] = console.handle_write
        r[Command.CMD_DATASTORE_PUT.value] = datastore.handle_put
        r[Command.CMD_DATASTORE_GET.value] = datastore.handle_get_request
        r[Command.CMD_MAILBOX_PUSH.value] = mailbox.handle_push
        r[Command.CMD_MAILBOX_AVAILABLE.value] = mailbox.handle_available
        r[Command.CMD_MAILBOX_READ.value] = mailbox.handle_read
        r[Command.CMD_MAILBOX_PROCESSED.value] = mailbox.handle_processed
        r[Command.CMD_FILE_WRITE.value] = file.handle_write
        r[Command.CMD_FILE_READ.value] = file.handle_read
        r[Command.CMD_FILE_REMOVE.value] = file.handle_remove
        r[Command.CMD_FILE_READ_RESP.value] = file.handle_read_response
        r[Command.CMD_PROCESS_RUN_ASYNC.value] = process.handle_run_async
        r[Command.CMD_PROCESS_POLL.value] = process.handle_poll
        r[Command.CMD_PROCESS_KILL.value] = process.handle_kill
        r[Command.CMD_DIGITAL_READ_RESP.value] = pin.handle_digital_read_resp
        r[Command.CMD_ANALOG_READ_RESP.value] = pin.handle_analog_read_resp
        r[Command.CMD_SPI_TRANSFER_RESP.value] = spi.handle_transfer_resp

        # Unhandled direct MCU requests
        r[Command.CMD_DIGITAL_READ.value] = (
            lambda s, p: pin.handle_unexpected_mcu_request(
                s, Command.CMD_DIGITAL_READ, p
            )
        )
        r[Command.CMD_ANALOG_READ.value] = (
            lambda s, p: pin.handle_unexpected_mcu_request(s, Command.CMD_ANALOG_READ, p)
        )

        # Status Handlers
        r[Status.ACK.value] = self._handle_ack
        for status in Status:
            if status != Status.ACK:
                r[status.value] = lambda s, p, st=status: self.handle_status(s, st, p)

        # 2. MQTT Topic Registry
        m = self._mqtt_router
        m.register(Topic.CONSOLE, console.handle_mqtt)
        m.register(Topic.DATASTORE, datastore.handle_mqtt)
        m.register(Topic.MAILBOX, mailbox.handle_mqtt)
        m.register(Topic.FILE, file.handle_mqtt)
        m.register(Topic.SHELL, process.handle_mqtt)
        m.register(Topic.DIGITAL, pin.handle_mqtt)
        m.register(Topic.ANALOG, pin.handle_mqtt)
        m.register(Topic.SPI, spi.handle_mqtt)
        m.register(Topic.SYSTEM, self._handle_mqtt_system)

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

        self._serial_sender = sender
        self._serial_flow.set_sender(sender)

    async def send_frame(
        self, command_id: int, payload: bytes = b"", seq_id: int | None = None
    ) -> bool:
        if not self._serial_sender:
            logger.error(
                "Serial sender not registered; cannot send frame 0x%02X",
                command_id,
            )
            return False
        return await self._serial_flow.send(command_id, payload)

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
            version_ok = await self._container.get(
                SystemComponent
            ).request_mcu_version()
            if not version_ok:
                logger.warning("Failed to dispatch MCU version request after reconnect")
        except (OSError, ValueError, RuntimeError) as e:
            logger.exception("Failed to request MCU version after reconnect: %s", e)

        try:
            await self._container.get(ConsoleComponent).flush_queue()
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
                "Serial link lost; clearing %d pending request(s) "
                "(digital=%d analog=%d)",
                total_pending,
                pending_digital,
                pending_analog,
            )

        self.state.pending_digital_reads.clear()
        self.state.pending_analog_reads.clear()

        # Ensure we do not keep the console in a paused state between links.
        self._container.get(ConsoleComponent).on_serial_disconnected()
        await self._serial_flow.reset()
        self.handshake_manager.clear_handshake_expectations()

    async def handle_mcu_frame(
        self, command_id: int, sequence_id: int, payload: bytes
    ) -> None:
        """Entry point invoked by the serial transport for each MCU frame (SIL-2)."""
        from ..state.context import resolve_command_id

        self._serial_flow.on_frame_received(command_id, sequence_id, payload)
        stats = self.state.serial_latency_stats
        start = time.perf_counter()

        try:
            if not self._is_mcu_frame_allowed_pre_sync(command_id):
                logger.warning(
                    "Security: Rejecting MCU frame 0x%02X (Link not synchronized)",
                    command_id,
                )
                return

            handler = self._mcu_registry.get(command_id)
            command_name = resolve_command_id(command_id)
            handled_successfully = False

            if handler:
                logger.debug(
                    "MCU > %s (seq=%d) [%d bytes]",
                    command_name,
                    sequence_id,
                    len(payload),
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

            if handled_successfully and command_id not in {
                status.value for status in Status
            }:
                await self.acknowledge_mcu_frame(command_id, sequence_id)

        finally:
            latency_ms = (time.perf_counter() - start) * 1000.0
            stats.record(latency_ms)

    async def handle_mqtt_message(self, inbound: Message) -> None:
        """Handle an inbound MQTT message (SIL-2)."""
        inbound_topic = str(inbound.topic)
        route = parse_topic(self.state.mqtt_topic_prefix, inbound_topic)
        if route is None or not route.segments:
            return

        start = time.perf_counter()
        try:
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

            # 3. Router Dispatch
            if not await self._mqtt_router.dispatch(route, inbound):
                logger.debug("Unhandled MQTT topic %s", inbound_topic)

        except (
            ValueError,
            TypeError,
            AttributeError,
            KeyError,
            IndexError,
            RuntimeError,
        ) as e:
            logger.critical(
                "Critical error processing MQTT message on topic %s: %s",
                inbound_topic,
                e,
                exc_info=True,
            )
        finally:
            latency_ms = (time.perf_counter() - start) * 1000.0
            self.state.record_rpc_latency_ms(latency_ms)

    def _is_mcu_frame_allowed_pre_sync(self, command_id: int) -> bool:
        return self.state.is_synchronized or command_id in _PRE_SYNC_ALLOWED_COMMANDS

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

    async def _handle_mqtt_system(self, route: TopicRoute, inbound: Message) -> bool:
        match route.identifier:
            case "bridge":
                match list(route.remainder):
                    case ["handshake", "get"]:
                        await self._publish_bridge_snapshot("handshake", inbound)
                        return True
                    case [("summary" | "state"), "get"]:
                        await self._publish_bridge_snapshot("summary", inbound)
                        return True
                    case _:
                        return False
            case _:
                return await self._container.get(SystemComponent).handle_mqtt(
                    route, inbound
                )
        return False

    async def enqueue_mqtt(
        self,
        message: QueuedPublish,
        *,
        reply_context: Message | None = None,
    ) -> None:
        """Enqueues an MQTT message for publishing with an overflow dropping strategy."""
        message_to_queue = message
        if reply_context is not None:
            props = reply_context.properties
            target_topic = (
                getattr(props, "ResponseTopic", None) if props else None
            ) or message.topic_name
            if target_topic != message_to_queue.topic_name:
                message_to_queue = msgspec.structs.replace(
                    message_to_queue, topic_name=target_topic
                )

            reply_correlation = (
                getattr(props, "CorrelationData", None) if props else None
            )
            if reply_correlation is not None:
                message_to_queue = msgspec.structs.replace(
                    message_to_queue, correlation_data=reply_correlation
                )

            origin_topic = str(reply_context.topic)
            user_properties = list(message_to_queue.user_properties)
            user_properties.append(("bridge-request-topic", origin_topic))
            message_to_queue = msgspec.structs.replace(
                message_to_queue, user_properties=user_properties
            )

        try:
            self.state.mqtt_publish_queue.put_nowait(message_to_queue)
        except (asyncio.QueueFull, asyncio.queues.QueueFull):
            # Dropping strategy: discard oldest, spool it, and insert new
            try:
                dropped = self.state.mqtt_publish_queue.get_nowait()
                self.state.mqtt_publish_queue.task_done()
                self.state.record_mqtt_drop(dropped.topic_name)

                # Use background task for spooling to avoid blocking enqueue
                await self.state.stash_mqtt_message(dropped)

                # Now the queue definitely has room
                self.state.mqtt_publish_queue.put_nowait(message_to_queue)

                logger.warning(
                    "MQTT publish queue saturated; dropped oldest message from topic=%s",
                    dropped.topic_name,
                )
            except (asyncio.QueueEmpty, asyncio.queues.QueueEmpty):
                # Race condition: someone else emptied it? Just retry insertion
                self.state.mqtt_publish_queue.put_nowait(message_to_queue)

    async def publish(
        self,
        topic: str,
        payload: bytes | str,
        *,
        qos: int = 0,
        retain: bool = False,
        expiry: int | None = None,
        properties: tuple[tuple[str, str], ...] = (),
        content_type: str | None = None,
        reply_to: Message | None = None,
    ) -> None:
        """Helper to enqueue an MQTT message without manually creating QueuedPublish."""
        if isinstance(payload, str):
            payload_bytes = payload.encode("utf-8")
        else:
            payload_bytes = payload

        message = QueuedPublish(
            topic_name=topic,
            payload=payload_bytes,
            qos=qos,
            retain=retain,
            content_type=content_type,
            message_expiry_interval=expiry,
            user_properties=list(properties or []),
        )
        await self.enqueue_mqtt(message, reply_context=reply_to)

    async def acknowledge_mcu_frame(
        self,
        command_id: int,
        seq_id: int,
        *,
        status: Status = Status.ACK,
    ) -> None:
        # [SIL-2] Use structured packet for acknowledgements
        payload = AckPacket(command_id=command_id).encode()
        if not self._serial_sender:
            logger.error(
                "Serial sender not registered; cannot emit status 0x%02X",
                status.value,
            )
            return

        try:
            await self._serial_sender(status.value, payload, seq_id)
        except (OSError, RuntimeError, ValueError) as exc:
            logger.warning(
                "Failed to enqueue status 0x%02X for command 0x%02X: %s",
                status.value,
                command_id,
                exc,
            )

    # --- MCU command handlers ---

    # ------------------------------------------------------------------
    # MCU -> Linux handlers
    # ------------------------------------------------------------------

    async def _handle_ack(self, seq_id: int, payload: bytes) -> None:
        if len(payload) >= 2:
            try:
                packet = AckPacket.decode(payload)
                command_id = packet.command_id
                logger.debug("MCU > ACK received for 0x%02X", command_id)
            except (msgspec.ValidationError, ValueError) as exc:
                logger.warning("MCU > Malformed ACK payload: %s", exc)
        else:
            logger.debug("MCU > ACK received")

    async def handle_status(self, seq_id: int, status: Status, payload: bytes) -> None:
        self.state.record_mcu_status(status)

        # [SIL-2] Improved status reporting with descriptive names
        desc = _STATUS_DESCRIPTIONS.get(status, "Unknown status code")
        text = payload.decode("utf-8", errors="ignore") if payload else ""

        log_method = (
            logger.warning if status not in {Status.OK, Status.ACK} else logger.debug
        )
        if text:
            log_method("MCU > %s (seq=%d): %s (%s)", status.name, seq_id, desc, text)
        else:
            log_method("MCU > %s (seq=%d): %s", status.name, seq_id, desc)

        report = _msgpack_enc.encode(
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
        await self.publish(
            topic=status_topic,
            payload=report,
            content_type="application/msgpack",
            expiry=MQTT_EXPIRY_SHELL,
            properties=tuple(properties),
        )

    # ------------------------------------------------------------------
    # Process management
    # ------------------------------------------------------------------

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
        await self.publish(
            topic=topic,
            payload=_msgpack_enc.encode(snapshot),
            content_type="application/msgpack",
            expiry=MQTT_EXPIRY_SHELL,
            properties=(("bridge-snapshot", flavor),),
            reply_to=inbound,
        )

    def _is_topic_action_allowed(
        self,
        topic_type: Topic | str,
        action: str,
    ) -> bool:
        if not action:
            return True
        topic_value = topic_type.value if isinstance(topic_type, Topic) else topic_type
        if self.state.topic_authorization:
            return self.state.topic_authorization.allows(topic_value, action)
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
        payload = _msgpack_enc.encode(
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
        await self.publish(
            topic=status_topic,
            payload=payload,
            content_type="application/msgpack",
            expiry=MQTT_EXPIRY_SHELL,
            properties=(("bridge-error", TOPIC_FORBIDDEN_REASON),),
            reply_to=inbound,
        )
