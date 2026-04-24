from __future__ import annotations

import asyncio
import structlog
from collections.abc import Coroutine
from typing import TYPE_CHECKING, Any

import msgspec
import svcs
from aiomqtt.message import Message

from ..config.const import MQTT_EXPIRY_SHELL, TOPIC_FORBIDDEN_REASON
from ..config.settings import RuntimeConfig
from ..protocol.protocol import Status
from ..protocol.structures import AckPacket
from ..protocol.topics import Topic, parse_topic, topic_path
from ..router.routers import MQTTRouter
from ..state.context import RuntimeState

if TYPE_CHECKING:
    from ..router.routers import McuHandler
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
    SendFrameCallable,
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
        self, config: RuntimeConfig, state: RuntimeState, mqtt_transport: Any
    ) -> None:
        self.config = config
        self.state = state
        self.mqtt_flow = mqtt_transport
        self._serial_timing: SerialTimingWindow = derive_serial_timing(config)
        self._task_group: asyncio.TaskGroup | None = None

        self._registry = svcs.Registry()

        # [SIL-2] Explicit component registration (Direct Access)
        # Eradicates the indirect factory loop to improve type traceability.
        self._registry.register_factory(
            ConsoleComponent,
            lambda: ConsoleComponent(
                config=config,
                state=state,
                serial_flow=self.serial_flow,
                mqtt_flow=self.mqtt_flow,
            ),
        )  # type: ignore[reportUnknownMemberType]
        self._registry.register_factory(
            DatastoreComponent,
            lambda: DatastoreComponent(
                config=config,
                state=state,
                serial_flow=self.serial_flow,
                mqtt_flow=self.mqtt_flow,
            ),
        )  # type: ignore[reportUnknownMemberType]
        self._registry.register_factory(
            FileComponent,
            lambda: FileComponent(
                config=config,
                state=state,
                serial_flow=self.serial_flow,
                mqtt_flow=self.mqtt_flow,
            ),
        )  # type: ignore[reportUnknownMemberType]
        self._registry.register_factory(
            MailboxComponent,
            lambda: MailboxComponent(
                config=config,
                state=state,
                serial_flow=self.serial_flow,
                mqtt_flow=self.mqtt_flow,
            ),
        )  # type: ignore[reportUnknownMemberType]
        self._registry.register_factory(
            PinComponent,
            lambda: PinComponent(
                config=config,
                state=state,
                serial_flow=self.serial_flow,
                mqtt_flow=self.mqtt_flow,
            ),
        )  # type: ignore[reportUnknownMemberType]
        self._registry.register_factory(
            ProcessComponent,
            lambda: ProcessComponent(
                config=config,
                state=state,
                serial_flow=self.serial_flow,
                mqtt_flow=self.mqtt_flow,
            ),
        )  # type: ignore[reportUnknownMemberType]
        self._registry.register_factory(
            SpiComponent,
            lambda: SpiComponent(
                config=config,
                state=state,
                serial_flow=self.serial_flow,
                mqtt_flow=self.mqtt_flow,
            ),
        )  # type: ignore[reportUnknownMemberType]
        self._registry.register_factory(
            SystemComponent,
            lambda: SystemComponent(
                config=config,
                state=state,
                serial_flow=self.serial_flow,
                mqtt_flow=self.mqtt_flow,
            ),
        )  # type: ignore[reportUnknownMemberType]

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
            enqueue_mqtt=mqtt_transport.enqueue_mqtt,
            acknowledge_frame=self.serial_flow.acknowledge,
            logger_=logger,
        )

        state.serial_ack_timeout_ms = self._serial_timing.ack_timeout_ms
        state.serial_response_timeout_ms = self._serial_timing.response_timeout_ms
        state.serial_retry_limit = self._serial_timing.retry_limit

        mcu_registry: dict[int, McuHandler] = {}
        self.dispatcher = BridgeDispatcher(
            mcu_registry=mcu_registry,
            mqtt_router=MQTTRouter(),
            state=state,
            send_frame=self.serial_flow.send,
            acknowledge_frame=self.serial_flow.acknowledge,
            is_topic_action_allowed=self._is_topic_action_allowed,
            reject_topic_action=self._reject_topic_action,
            publish_bridge_snapshot=self._publish_bridge_snapshot,
            on_frame_received=self.serial_flow.on_frame_received,
        )
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
                "Serial link lost; clearing %d pending request(s) (digital=%d analog=%d)",
                total_pending,
                pending_digital,
                pending_analog,
            )

        self.state.pending_digital_reads.clear()
        self.state.pending_analog_reads.clear()

        # Ensure we do not keep the console in a paused state between links.
        self._container.get(ConsoleComponent).on_serial_disconnected()
        await self.serial_flow.reset()
        self.handshake_manager.clear_handshake_expectations()

    async def handle_mcu_frame(
        self, command_id: int, sequence_id: int, payload: bytes
    ) -> None:
        """Entry point invoked by the serial transport for each MCU frame."""
        await self.dispatcher.dispatch_mcu_frame(command_id, sequence_id, payload)

    async def handle_mqtt_message(self, inbound: Message) -> None:
        """Entry point invoked by the MQTT transport for each inbound message."""
        await self.dispatcher.dispatch_mqtt_message(
            inbound,
            lambda t: parse_topic(self.state.mqtt_topic_prefix, t),
        )

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
        await self.mqtt_flow.publish(
            topic=status_topic,
            payload=report,
            content_type="application/msgpack",
            expiry=MQTT_EXPIRY_SHELL,
            properties=tuple(properties),
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
        await self.mqtt_flow.publish(
            topic=topic,
            payload=msgspec.msgpack.encode(snapshot),
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
        await self.mqtt_flow.publish(
            topic=status_topic,
            payload=payload,
            content_type="application/msgpack",
            expiry=MQTT_EXPIRY_SHELL,
            properties=(("bridge-error", TOPIC_FORBIDDEN_REASON),),
            reply_to=inbound,
        )
