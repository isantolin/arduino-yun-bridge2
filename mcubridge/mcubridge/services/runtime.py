from __future__ import annotations

import asyncio
import structlog
import time
from collections.abc import Coroutine
from typing import TYPE_CHECKING, Any, cast

import msgspec
import svcs
from aiomqtt.message import Message

from ..config.const import MQTT_EXPIRY_SHELL, TOPIC_FORBIDDEN_REASON
from ..config.settings import RuntimeConfig
from ..mqtt import parse_topic
from ..protocol import protocol
from ..protocol.protocol import Status, Command, Topic
from ..protocol.topics import topic_path
from ..protocol.structures import QueuedPublish
from ..state.context import RuntimeState
from .dispatcher import BridgeDispatcher
from .handshake import SerialHandshakeManager, SerialTimingWindow, derive_serial_timing
from .serial_flow import SerialFlowController
from .console import ConsoleComponent
from .datastore import DatastoreComponent
from .file import FileComponent
from .mailbox import MailboxComponent
from .pin import PinComponent
from .process import ProcessComponent
from .spi import SpiComponent
from .system import SystemComponent
from ..mqtt.spool_manager import MqttSpoolManager

logger = structlog.get_logger("mcubridge.runtime")


class BridgeService:
    """Service façade orchestrating MCU and MQTT interactions. [SIL-2]"""

    def __init__(
        self, 
        config: RuntimeConfig, 
        state: RuntimeState, 
        spool_manager: MqttSpoolManager
    ) -> None:
        self.config = config
        self.state = state
        self.spool_manager = spool_manager
        self.mqtt_flow = self # Alias for compatibility
        self._serial_timing: SerialTimingWindow = derive_serial_timing(config)
        self._task_group: asyncio.TaskGroup | None = None

        self._registry = svcs.Registry()

        # [SIL-2] Explicit component registration (Direct Access)
        reg = cast(Any, self._registry)
        for comp_type in (
            ConsoleComponent, DatastoreComponent, FileComponent, 
            MailboxComponent, PinComponent, ProcessComponent, 
            SpiComponent, SystemComponent
        ):
            reg.register_factory(
                comp_type,
                lambda c=comp_type: c(
                    config=config,
                    state=state,
                    serial_flow=self.serial_flow,
                    mqtt_flow=self,
                ),
            )

        self._container = svcs.Container(self._registry)

        self.serial_flow = SerialFlowController(
            ack_timeout=self._serial_timing.ack_timeout_seconds,
            response_timeout=self._serial_timing.response_timeout_seconds,
            max_attempts=self._serial_timing.retry_limit,
            logger=logger,
        )
        self.serial_flow.set_metrics_callback(state.record_serial_flow_event)
        self.serial_flow.set_pipeline_observer(state.record_serial_pipeline_event)

        self.handshake_manager = SerialHandshakeManager(
            config=config,
            state=state,
            serial_timing=self._serial_timing,
            send_frame=self.serial_flow.send,
            enqueue_mqtt=self.enqueue_mqtt,
            acknowledge_frame=self.serial_flow.acknowledge,
            logger_=logger,
        )

        state.serial_ack_timeout_ms = self._serial_timing.ack_timeout_ms
        state.serial_response_timeout_ms = self._serial_timing.response_timeout_ms
        state.serial_retry_limit = self._serial_timing.retry_limit

        mcu_registry: dict[int, Any] = {}
        self.dispatcher = BridgeDispatcher(
            mcu_registry=mcu_registry,
            mqtt_router=None, # Will be set below
            state=state,
            send_frame=self.serial_flow.send,
            acknowledge_frame=self.serial_flow.acknowledge,
            is_topic_action_allowed=self._is_topic_action_allowed,
            reject_topic_action=self._reject_topic_action,
            publish_bridge_snapshot=self._publish_bridge_snapshot,
            on_frame_received=self.serial_flow.on_frame_received,
        )
        from ..router.routers import MQTTRouter
        self.dispatcher.mqtt_router = MQTTRouter()
        
        # Sychronous registration
        self.dispatcher.register_components(self._container)
        
        self.dispatcher.register_system_handlers(
            handle_link_sync_resp=self.handshake_manager.handle_link_sync_resp,
            handle_link_reset_resp=self.handshake_manager.handle_link_reset_resp,
            handle_get_capabilities_resp=self.handshake_manager.handle_capabilities_resp,
            handle_ack=self._handle_ack,
            status_handler_factory=lambda status: lambda s, p: self.handle_status(
                s, status, p
            ),
            handle_process_kill=self._container.get(ProcessComponent).handle_kill,
        )

    async def __aenter__(self) -> BridgeService:
        self._task_group = asyncio.TaskGroup()
        await self._task_group.__aenter__()
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc_val: BaseException | None,
        exc_tb: Any | None,
    ) -> None:
        if self._task_group:
            await self._task_group.__aexit__(exc_type, exc_val, exc_tb)
        await self._container.aclose()

    def register_serial_sender(
        self, sender: Callable[[int, bytes, int | None], Coroutine[Any, Any, bool]]
    ) -> None:
        """Register the underlying serial transport's send method."""
        self.serial_flow.set_sender(sender)

    async def on_serial_connected(self) -> None:
        """Lifecycle hook: Serial transport is established."""
        try:
            await self.handshake_manager.synchronize()
            self._container.get(ConsoleComponent).flush_queue()
        except Exception as e:
            logger.error("Error in on_serial_connected hook: %s", e)

    async def on_serial_disconnected(self) -> None:
        """Lifecycle hook: Serial transport is closed."""
        self.state.mark_transport_disconnected()
        self.serial_flow.abandon_pending()

    async def handle_mcu_frame(self, cmd_id: int, seq_id: int, payload: bytes) -> None:
        """Dispatches an inbound MCU frame through the dispatcher."""
        await self.dispatcher.handle_mcu_frame(cmd_id, seq_id, payload)

    async def handle_mqtt_message(self, inbound: Message) -> None:
        """Dispatches an inbound MQTT message through the dispatcher."""
        await self.dispatcher.dispatch_mqtt_message(
            inbound,
            lambda t: parse_topic(self.state.mqtt_topic_prefix, t),
        )

    async def enqueue_mqtt(
        self,
        message: QueuedPublish,
        *,
        reply_context: Message | None = None,
    ) -> None:
        """Enqueues an MQTT message for publishing."""
        target_message = message
        
        if reply_context is not None and reply_context.properties:
            props = reply_context.properties
            target_topic = getattr(props, "ResponseTopic", None) or message.topic_name
            correlation = getattr(props, "CorrelationData", None)
            
            updates: dict[str, Any] = {}
            if target_topic != message.topic_name:
                updates["topic_name"] = target_topic
            if correlation is not None:
                updates["correlation_data"] = correlation
            
            user_props = list(message.user_properties)
            user_props.append(("bridge-request-topic", str(reply_context.topic)))
            updates["user_properties"] = tuple(user_props)
            
            if updates:
                target_message = msgspec.structs.replace(message, **updates)

        try:
            self.state.mqtt_publish_queue.put_nowait(target_message)
        except asyncio.QueueFull:
            try:
                dropped = self.state.mqtt_publish_queue.get_nowait()
                self.state.mqtt_publish_queue.task_done()
                self.state.mqtt_dropped_messages += 1
                self.state.metrics.mqtt_messages_dropped.inc()
                await self.spool_manager.stash(dropped)
                self.state.mqtt_publish_queue.put_nowait(target_message)
            except asyncio.QueueEmpty:
                self.state.mqtt_publish_queue.put_nowait(target_message)

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
        """Convenience method for publishing."""
        payload_bytes = payload.encode("utf-8") if isinstance(payload, str) else payload
        message = QueuedPublish(
            topic_name=topic,
            payload=payload_bytes,
            qos=qos,
            retain=retain,
            content_type=content_type,
            message_expiry_interval=expiry,
            user_properties=tuple(properties or ()),
        )
        await self.enqueue_mqtt(message, reply_context=reply_to)

    async def _handle_ack(self, seq_id: int, payload: bytes) -> None:
        """Handle CMD_ACK from MCU."""
        if len(payload) >= 2:
            try:
                ack = msgspec.msgpack.decode(payload, type=protocol.AckPacket)
                self.serial_flow.on_ack_received(ack.command_id, seq_id)
            except (ValueError, msgspec.MsgspecError):
                pass

    async def handle_status(self, seq_id: int, status: Status, payload: bytes) -> None:
        """Handle a status report frame from the MCU."""
        text = payload.decode("utf-8", errors="replace") if payload else ""
        report = {
            "status": status.value,
            "name": status.name,
            "message": text,
            "timestamp": time.time(),
        }
        status_topic = topic_path(self.state.mqtt_topic_prefix, Topic.SYSTEM, Topic.STATUS)
        properties = [("bridge-status", status.name), ("bridge-status-description", status.name)]
        if text:
            properties.append(("bridge-status-message", text))
            
        await self.publish(
            topic=status_topic,
            payload=msgspec.msgpack.encode(report),
            content_type="application/msgpack",
            expiry=MQTT_EXPIRY_SHELL,
            properties=tuple(properties),
        )

    def _is_topic_action_allowed(self, topic: Topic, action: str) -> bool:
        if self.state.topic_authorization is None:
            return True
        # Coerce to name check if needed
        return getattr(self.state.topic_authorization, f"{topic.name.lower()}_{action}", True)

    async def _reject_topic_action(self, inbound: Message, topic: Topic, action: str) -> None:
        payload = msgspec.msgpack.encode({
            "status": Status.STATUS_FORBIDDEN.value if hasattr(Status, "STATUS_FORBIDDEN") else 403,
            "error": TOPIC_FORBIDDEN_REASON,
            "topic": str(inbound.topic),
            "action": action,
        })
        status_topic = topic_path(self.state.mqtt_topic_prefix, Topic.SYSTEM, Topic.STATUS)
        await self.publish(
            topic=status_topic,
            payload=payload,
            content_type="application/msgpack",
            expiry=MQTT_EXPIRY_SHELL,
            properties=(("bridge-error", TOPIC_FORBIDDEN_REASON),),
            reply_to=inbound,
        )

    async def _publish_bridge_snapshot(self, flavor: str, inbound: Message | None) -> None:
        # Fallback if get_snapshot is not present on the mock
        snapshot = getattr(self.state, "get_snapshot", lambda f: {})(flavor)
        topic_segments = ["bridge", "snapshot", flavor]
        topic = topic_path(self.state.mqtt_topic_prefix, Topic.SYSTEM, *topic_segments)
        await self.publish(
            topic=topic,
            payload=msgspec.msgpack.encode(snapshot),
            content_type="application/msgpack",
            expiry=MQTT_EXPIRY_SHELL,
            properties=(("bridge-snapshot", flavor),),
            reply_to=inbound,
        )
