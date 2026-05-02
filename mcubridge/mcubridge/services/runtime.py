from __future__ import annotations

import asyncio
import structlog
from collections.abc import Callable, Awaitable
from typing import TYPE_CHECKING, Any

import msgspec
from aiomqtt.message import Message

from ..config.const import MQTT_EXPIRY_SHELL, TOPIC_FORBIDDEN_REASON
from ..config.settings import RuntimeConfig
from ..protocol import protocol
from ..protocol.protocol import Status
from ..protocol.structures import QueuedPublish, TopicRoute
from ..protocol.topics import Topic, parse_topic, topic_path
from ..state.context import RuntimeState

if TYPE_CHECKING:
    pass

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
from .dispatcher import BridgeDispatcher
from .handshake import (
    SerialHandshakeManager,
    SerialTimingWindow,
    derive_serial_timing,
)
from .serial_flow import SerialFlowController

logger = structlog.get_logger("mcubridge.service")

STATUS_VALUES = {status.value for status in Status}


class BridgeService:
    """Service façade orchestrating MCU and MQTT interactions. [SIL-2]"""

    def __init__(
        self,
        config: RuntimeConfig,
        state: RuntimeState,
        enqueue_mqtt: Callable[..., Awaitable[None]] | Any,
    ) -> None:
        self.config = config
        self.state = state
        self.enqueue_mqtt = enqueue_mqtt
        self._serial_timing: SerialTimingWindow = derive_serial_timing(config)
        self._task_group: asyncio.TaskGroup | None = None

        self.serial_flow = SerialFlowController(
            ack_timeout=self._serial_timing.ack_timeout_seconds,
            response_timeout=self._serial_timing.response_timeout_seconds,
            max_attempts=self._serial_timing.retry_limit,
            logger=logger,
        )
        self.serial_flow.set_metrics_callback(state.record_serial_flow_event)
        self.serial_flow.set_pipeline_observer(state.record_serial_pipeline_event)

        # [SIL-2] Explicit component instantiation (Zero-Wrapper)
        self.console = ConsoleComponent(
            config, state, self.serial_flow, self.enqueue_mqtt
        )
        self.datastore = DatastoreComponent(
            config, state, self.serial_flow, self.enqueue_mqtt
        )
        self.file = FileComponent(config, state, self.serial_flow, self.enqueue_mqtt)
        self.mailbox = MailboxComponent(
            config, state, self.serial_flow, self.enqueue_mqtt
        )
        self.pin = PinComponent(config, state, self.serial_flow, self.enqueue_mqtt)
        self.process = ProcessComponent(
            config, state, self.serial_flow, self.enqueue_mqtt
        )
        self.spi = SpiComponent(config, state, self.serial_flow, self.enqueue_mqtt)
        self.system = SystemComponent(
            config, state, self.serial_flow, self.enqueue_mqtt
        )

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
            state=state,
            send_frame=self.serial_flow.send,
            acknowledge_frame=self.serial_flow.acknowledge,
            is_topic_action_allowed=self._is_topic_action_allowed,
            reject_topic_action=self._reject_topic_action,
            publish_bridge_snapshot=self._publish_bridge_snapshot,
            on_frame_received=self.serial_flow.on_frame_received,
        )
        self.dispatcher.register_components(
            console=self.console,
            datastore=self.datastore,
            file=self.file,
            mailbox=self.mailbox,
            pin=self.pin,
            process=self.process,
            spi=self.spi,
            system=self.system,
        )
        self.dispatcher.register_system_handlers(
            handle_link_sync_resp=self.handshake_manager.handle_link_sync_resp,
            handle_link_reset_resp=self.handshake_manager.handle_link_reset_resp,
            handle_get_capabilities_resp=self.handshake_manager.handle_capabilities_resp,
            handle_ack=self._handle_ack,
            status_handler_factory=lambda status: lambda s, p: self.handle_status(
                s, status, p
            ),
            handle_process_kill=self.process.handle_kill,
        )

    async def __aenter__(self) -> BridgeService:
        self._task_group = asyncio.TaskGroup()
        await self._task_group.__aenter__()
        return self

    async def __aexit__(self, exc_type: Any, exc_val: Any, exc_tb: Any) -> None:
        if self._task_group is not None:
            await self._task_group.__aexit__(exc_type, exc_val, exc_tb)
            self._task_group = None

    async def on_serial_connected(self) -> None:
        """Triggered by transport once the serial link is established."""

        self.state.mark_transport_connected()

        # [SIL-2] Non-blocking handshake initiation
        try:
            await self.handshake_manager.synchronize()
            self.handshake_manager.raise_if_handshake_fatal()
        except (OSError, RuntimeError) as e:
            logger.exception("Handshake failed to start: %s", e)
            return

        if not self.state.is_synchronized:
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
        await self.console.on_serial_disconnected()
        await self.serial_flow.reset()
        self.handshake_manager.clear_handshake_expectations()

    async def handle_mcu_frame(
        self, command_id: int, sequence_id: int, payload: bytes
    ) -> None:
        """Central entry point for all frames received from the MCU."""
        # [SIL-2] Use dispatcher for O(1) jump table routing
        await self.dispatcher.dispatch_mcu_frame(command_id, sequence_id, payload)

    async def handle_mqtt_message(self, inbound: Message) -> None:
        """Central entry point for all messages received from MQTT."""
        # [SIL-2] Pass parse_topic to dispatcher to decouple network from logic
        await self.dispatcher.dispatch_mqtt_message(inbound, self._parse_inbound_topic)

    def _parse_inbound_topic(self, topic: str) -> TopicRoute | None:
        return parse_topic(self.state.mqtt_topic_prefix, topic)

    async def _handle_ack(self, sequence_id: int, _: bytes) -> None:
        """Handle CMD_ACK received from MCU."""
        self.serial_flow.on_frame_received(Status.ACK.value, sequence_id, b"")

    async def handle_status(
        self, sequence_id: int, status: Status, payload: bytes
    ) -> None:
        """Relay MCU status codes to MQTT for remote monitoring."""
        status_topic = topic_path(
            self.state.mqtt_topic_prefix, Topic.SYSTEM, "mcu_status"
        )

        self.state.mcu_status_counts[status.name] = (
            self.state.mcu_status_counts.get(status.name, 0) + 1
        )
        self.state.metrics.mcu_status_counts.labels(status=status.name).inc()

        await self.enqueue_mqtt(
            QueuedPublish(
                topic_name=status_topic,
                payload=payload,
                content_type="application/msgpack",
                message_expiry_interval=MQTT_EXPIRY_SHELL,
                user_properties=(("bridge-error", TOPIC_FORBIDDEN_REASON),),
            ),
        )

    def _is_topic_action_allowed(self, topic: Topic, action: str) -> bool:
        if self.state.topic_authorization is None:
            return True
        return self.state.topic_authorization.allows(topic.value, action)

    async def _reject_topic_action(
        self, inbound: Message, topic: Topic, action: str
    ) -> None:
        response_topic = topic_path(
            self.state.mqtt_topic_prefix, topic, action, protocol.MQTT_SUFFIX_RESPONSE
        )
        await self.enqueue_mqtt(
            QueuedPublish(
                topic_name=response_topic,
                payload=TOPIC_FORBIDDEN_REASON.encode(),
                user_properties=(("bridge-error", TOPIC_FORBIDDEN_REASON),),
            ),
            reply_context=inbound,
        )

    async def _publish_bridge_snapshot(
        self, category: str, inbound: Message | None
    ) -> None:
        snapshot = self.state.build_bridge_snapshot()
        data = (
            msgspec.structs.asdict(snapshot)
            if category == "summary"
            else msgspec.structs.asdict(snapshot.handshake)
        )
        payload = msgspec.msgpack.encode(data)

        topic = topic_path(
            self.state.mqtt_topic_prefix, Topic.SYSTEM, "bridge", category
        )
        await self.enqueue_mqtt(
            QueuedPublish(
                topic_name=topic,
                payload=payload,
                content_type="application/msgpack",
                message_expiry_interval=MQTT_EXPIRY_SHELL,
            ),
            reply_context=inbound,
        )


__all__ = ["BridgeService"]
