from __future__ import annotations

import asyncio
import msgspec
import logging
import time
from collections.abc import Awaitable, Callable, Coroutine
from typing import Any, cast

from aiomqtt.message import Message

from ..config.settings import RuntimeConfig
from ..const import TOPIC_FORBIDDEN_REASON
from ..mqtt.messages import QueuedPublish
from ..protocol.topics import Topic, TopicRoute, parse_topic, topic_path
from ..rpc import protocol
from ..rpc.protocol import Status  # Only Status from rpc.protocol needed

from ..state.context import RuntimeState
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
from .dispatcher import BridgeDispatcher
from .handshake import (
    SerialHandshakeFatal,
    SerialHandshakeManager,
    SerialTimingWindow,
    SendFrameCallable,
    derive_serial_timing,
)
from .routers import MCUHandlerRegistry, MQTTRouter
from .serial_flow import SerialFlowController

logger = logging.getLogger("mcubridge.service")


async def _background_task_runner(
    coroutine: Coroutine[Any, Any, None],
    *,
    task_name: str | None,
) -> None:
    # Exceptions bubble up to the TaskGroup, which cancels siblings and
    # raises ExceptionGroup in the main loop.
    await coroutine


class _StatusHandler:
    def __init__(self, service: "BridgeService", status: Status) -> None:
        self._service = service
        self._status = status

    async def __call__(self, payload: bytes) -> None:
        await self._service.handle_status(self._status, payload)


STATUS_VALUES = {status.value for status in Status}
_PRE_SYNC_ALLOWED_COMMANDS = {
    protocol.Command.CMD_LINK_SYNC_RESP.value,
    protocol.Command.CMD_LINK_RESET_RESP.value,
}

_MAX_PAYLOAD_BYTES = int(protocol.MAX_PAYLOAD_SIZE)
_STATUS_PAYLOAD_WINDOW = max(0, _MAX_PAYLOAD_BYTES - 2)


class BridgeService:
    """Service façade orchestrating MCU and MQTT interactions.

    This class acts as the central business logic layer for the MCU Bridge daemon,
    decoupling the transport mechanisms (serial and MQTT) from the command
    processing and state management. It handles:

    -   Dispatching incoming MCU frames to appropriate component handlers.
    -   Routing incoming MQTT messages to their respective handlers.
    -   Managing the serial link handshake and flow control.
    -   Orchestrating various components (Console, Datastore, File, Mailbox, Pin, Process, Shell, System)
        by providing them with necessary context and a communication channel.
    -   Managing background tasks related to the bridge's operation.

    It relies on `RuntimeConfig` for configuration, `RuntimeState` for
    managing transient and persistent state, and various component classes
    to encapsulate specific functionalities.
    """

    def __init__(self, config: RuntimeConfig, state: RuntimeState) -> None:
        self.config = config
        self.state = state
        self._serial_sender: SendFrameCallable | None = None
        self._serial_timing: SerialTimingWindow = derive_serial_timing(config)
        self._task_group: asyncio.TaskGroup | None = None

        self._console = ConsoleComponent(config, state, self)
        self._datastore = DatastoreComponent(config, state, self)
        self._file = FileComponent(config, state, self)
        self._mailbox = MailboxComponent(config, state, self)
        self._pin = PinComponent(config, state, self)
        self._process = ProcessComponent(config, state, self)
        self._shell = ShellComponent(config, state, self, self._process)
        self._system = SystemComponent(config, state, self)

        self._handshake = SerialHandshakeManager(
            config=config,
            state=state,
            serial_timing=self._serial_timing,
            send_frame=self.send_frame,
            enqueue_mqtt=self.enqueue_mqtt,
            acknowledge_frame=self._acknowledge_mcu_frame,
            logger_=logger,
        )

        self._dispatcher = BridgeDispatcher(
            mcu_registry=MCUHandlerRegistry(),
            mqtt_router=MQTTRouter(),
            send_frame=self.send_frame,
            acknowledge_frame=self._acknowledge_mcu_frame,
            is_link_synchronized=self.is_link_synchronized,
            is_topic_action_allowed=self._is_topic_action_allowed,
            reject_topic_action=self._reject_topic_action,
            publish_bridge_snapshot=self._publish_bridge_snapshot,
        )
        self._dispatcher.register_components(
            console=self._console,
            datastore=self._datastore,
            file=self._file,
            mailbox=self._mailbox,
            pin=self._pin,
            process=self._process,
            shell=self._shell,
            system=self._system,
        )
        self._dispatcher.register_system_handlers(
            handle_link_sync_resp=self._handshake.handle_link_sync_resp,
            handle_link_reset_resp=self._handshake.handle_link_reset_resp,
            handle_get_capabilities_resp=self._handshake.handle_capabilities_resp,
            handle_ack=self._handle_ack,
            status_handler_factory=self._status_handler,
            handle_process_kill=self._process.handle_kill,
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

    async def send_frame(self, command_id: int, payload: bytes = b"") -> bool:
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

        return self._task_group.create_task(
            _background_task_runner(coroutine, task_name=name),
            name=name,
        )

    def is_link_synchronized(self) -> bool:
        return self.state.link_is_synchronized

    def parse_inbound_topic(self, topic_name: str) -> TopicRoute | None:
        return parse_topic(self.state.mqtt_topic_prefix, topic_name)

    def compute_handshake_tag(self, nonce: bytes) -> bytes:
        return self._handshake.compute_handshake_tag(nonce)

    def trim_process_buffers(
        self, stdout_buffer: bytearray, stderr_buffer: bytearray
    ) -> tuple[bytes, bytes, bool, bool]:
        return self._process.trim_buffers(stdout_buffer, stderr_buffer)

    async def on_serial_connected(self) -> None:
        """Run post-connection initialisation for the MCU link."""

        self.state.serial_link_connected = True
        handshake_ok = False
        fatal_error: SerialHandshakeFatal | None = None
        try:
            handshake_ok = await self.sync_link()
        except SerialHandshakeFatal as exc:
            fatal_error = exc
            handshake_ok = False
        except (OSError, ValueError, RuntimeError) as e:
            logger.exception("Failed to synchronise MCU link after reconnect: %s", e)

        if fatal_error is not None:
            raise fatal_error

        if not handshake_ok:
            self._handshake.raise_if_handshake_fatal()
            logger.error("Skipping post-connect initialisation because MCU link " "sync failed")
            return

        try:
            version_ok = await self._system.request_mcu_version()
            if not version_ok:
                logger.warning("Failed to dispatch MCU version request after reconnect")
        except (OSError, ValueError, RuntimeError) as e:
            logger.exception("Failed to request MCU version after reconnect: %s", e)

        try:
            await self._console.flush_queue()
        except (OSError, ValueError, RuntimeError) as e:
            logger.exception("Failed to flush console backlog after reconnect: %s", e)

    async def on_serial_disconnected(self) -> None:
        """Reset transient MCU tracking when the serial link drops."""

        self.state.serial_link_connected = False

        pending_digital = len(self.state.pending_digital_reads)
        pending_analog = len(self.state.pending_analog_reads)

        total_pending = pending_digital + pending_analog
        if total_pending:
            logger.warning(
                "Serial link lost; clearing %d pending request(s) " "(digital=%d analog=%d)",
                total_pending,
                pending_digital,
                pending_analog,
            )

        self.state.pending_digital_reads.clear()
        self.state.pending_analog_reads.clear()

        # Ensure we do not keep the console in a paused state between links.
        self._console.on_serial_disconnected()
        await self._serial_flow.reset()
        self._handshake.clear_handshake_expectations()

    async def handle_mcu_frame(self, command_id: int, payload: bytes) -> None:
        """Entry point invoked by the serial transport for each MCU frame."""

        self._serial_flow.on_frame_received(command_id, payload)
        try:
            await self._dispatcher.dispatch_mcu_frame(command_id, payload)
        except (OSError, ValueError, TypeError, AttributeError, RuntimeError) as e:
            logger.critical(
                "Critical error handling MCU frame: CMD=0x%02X payload=%s: %s",
                command_id,
                payload.hex(),
                e,
                exc_info=True,
            )

    async def handle_mqtt_message(self, inbound: Message) -> None:
        inbound_topic = str(inbound.topic)
        try:
            await self._dispatcher.dispatch_mqtt_message(
                inbound,
                self.parse_inbound_topic,
            )
        except (OSError, ValueError, TypeError, AttributeError, RuntimeError) as e:
            logger.critical(
                "Critical error processing MQTT message on topic %s: %s",
                inbound_topic,
                e,
                exc_info=True,
            )

    async def enqueue_mqtt(
        self,
        message: QueuedPublish,
        *,
        reply_context: Message | None = None,
    ) -> None:
        """Enqueues an MQTT message for publishing.

        This method adds a `QueuedPublish` message to the internal MQTT publish queue.
        It handles optional `reply_context` to infer response topics and correlation data
        for MQTT 5 response-request patterns. If the queue is saturated, it implements
        a dropping strategy: the oldest message is dropped and, if possible, spooled
        to persistent storage to prevent data loss during temporary broker unavailability.

        Args:
            message: The `QueuedPublish` object to enqueue.
            reply_context: An optional `Message` that triggered this publish,
                           used to derive `ResponseTopic` and `CorrelationData` for replies.
        """
        message_to_queue = message
        if reply_context is not None:
            props = getattr(reply_context, "properties", None)
            resp_topic = getattr(props, "ResponseTopic", None) if props else None
            target_topic = resp_topic or message.topic_name
            if target_topic != message_to_queue.topic_name:
                message_to_queue = msgspec.structs.replace(
                    message_to_queue,
                    topic_name=target_topic,
                )
            reply_correlation = getattr(props, "CorrelationData", None) if props else None
            if reply_correlation is not None:
                message_to_queue = msgspec.structs.replace(
                    message_to_queue,
                    correlation_data=reply_correlation,
                )
            origin_topic = str(reply_context.topic)
            user_properties = message_to_queue.user_properties + (("bridge-request-topic", origin_topic),)
            message_to_queue = msgspec.structs.replace(
                message_to_queue,
                user_properties=user_properties,
            )

        while True:
            try:
                self.state.mqtt_publish_queue.put_nowait(message_to_queue)
                return
            except asyncio.QueueFull:
                try:
                    dropped = self.state.mqtt_publish_queue.get_nowait()
                except asyncio.QueueEmpty:
                    await asyncio.sleep(0)
                    continue

                self.state.mqtt_publish_queue.task_done()
                drop_topic = dropped.topic_name
                self.state.record_mqtt_drop(drop_topic)
                stored = await self.state.stash_mqtt_message(dropped)
                spool_note: str
                if stored:
                    pending = self.state.mqtt_spool.pending if self.state.mqtt_spool is not None else 0
                    spool_note = f"; spooled_pending={pending}"
                else:
                    reason = self.state.mqtt_spool_failure_reason or "unknown"
                    backoff_remaining = max(
                        0.0,
                        self.state.mqtt_spool_backoff_until - time.monotonic(),
                    )
                    spool_note = "; spool_unavailable reason=%s backoff_remaining=%.1fs" % (reason, backoff_remaining)
                logger.warning(
                    "MQTT publish queue saturated (%d/%d); dropping oldest " "topic=%s%s",
                    self.state.mqtt_publish_queue.qsize(),
                    self.state.mqtt_queue_limit,
                    drop_topic,
                    spool_note,
                )

    async def sync_link(self) -> bool:
        return await self._handshake.synchronize()

    async def _handle_handshake_failure(
        self,
        reason: str,
        *,
        detail: str | None = None,
    ) -> None:
        await self._handshake.handle_handshake_failure(
            reason,
            detail=detail,
        )

    async def _acknowledge_mcu_frame(
        self,
        command_id: int,
        *,
        status: Status = Status.ACK,
        extra: bytes = b"",
    ) -> None:
        payload = cast(Any, protocol.UINT16_STRUCT).build(command_id)
        if extra:
            remaining = _MAX_PAYLOAD_BYTES - len(payload)
            if remaining > 0:
                payload += extra[:remaining]
        if not self._serial_sender:
            logger.error(
                "Serial sender not registered; cannot emit status 0x%02X",
                status.value,
            )
            return
        try:
            await self._serial_sender(status.value, payload)
        except (OSError, ValueError) as exc:
            logger.error(
                "Failed to emit status 0x%02X for command 0x%02X: %s",
                status.value,
                command_id,
                exc,
            )

    # --- MCU command handlers ---

    # ------------------------------------------------------------------
    # MCU -> Linux handlers
    # ------------------------------------------------------------------

    async def _handle_ack(self, payload: bytes) -> None:
        if len(payload) >= 2:
            command_id = int.from_bytes(payload[:2], "big")
            logger.debug("MCU > ACK received for 0x%02X", command_id)
        else:
            logger.debug("MCU > ACK received")

    def _status_handler(self, status: Status) -> Callable[[bytes], Awaitable[None]]:
        return _StatusHandler(self, status)

    async def handle_status(self, status: Status, payload: bytes) -> None:
        self.state.record_mcu_status(status)
        text = payload.decode("utf-8", errors="ignore") if payload else ""
        log_method = logger.warning if status != Status.ACK else logger.debug
        log_method("MCU > %s %s", status.name, text)

        report = msgspec.json.encode(
            {
                "status": status.value,
                "name": status.name,
                "message": text,
            }
        )
        status_topic = topic_path(
            self.state.mqtt_topic_prefix,
            Topic.SYSTEM,
            Topic.STATUS,
        )
        properties: list[tuple[str, str]] = [("bridge-status", status.name)]
        if text:
            properties.append(("bridge-status-message", text))
        message = QueuedPublish(
            topic_name=status_topic,
            payload=report,
            content_type="application/json",
            message_expiry_interval=30,
            user_properties=tuple(properties),
        )
        await self.enqueue_mqtt(message)

    # ------------------------------------------------------------------
    # Process management
    # ------------------------------------------------------------------

    def is_command_allowed(self, command: str) -> bool:
        return self.state.allowed_policy.is_allowed(command)

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
        message = QueuedPublish(
            topic_name=topic,
            payload=msgspec.json.encode(snapshot),
            content_type="application/json",
            message_expiry_interval=30,
            user_properties=(("bridge-snapshot", flavor),),
        )
        await self.enqueue_mqtt(message, reply_context=inbound)

    def _is_topic_action_allowed(
        self,
        topic_type: Topic | str,
        action: str,
    ) -> bool:
        if not action:
            return True
        topic_value = topic_type.value if isinstance(topic_type, Topic) else topic_type
        return self.state.topic_authorization.allows(topic_value, action)

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
        payload = msgspec.json.encode(
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
        message = QueuedPublish(
            topic_name=status_topic,
            payload=payload,
            content_type="application/json",
            message_expiry_interval=30,
            user_properties=(("bridge-error", TOPIC_FORBIDDEN_REASON),),
        )
        await self.enqueue_mqtt(message, reply_context=inbound)
