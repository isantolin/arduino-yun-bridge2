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
from ..protocol.protocol import Status  # Only Status from rpc.protocol needed
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
_msgpack_enc = msgspec.msgpack.Encoder()


STATUS_VALUES = {status.value for status in Status}


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

    def __init__(self, config: RuntimeConfig, state: RuntimeState, mqtt_transport: Any) -> None:
        self.config = config
        self.state = state
        self._mqtt_transport = mqtt_transport
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

        self._serial_flow = SerialFlowController(
            ack_timeout=self._serial_timing.ack_timeout_seconds,
            response_timeout=self._serial_timing.response_timeout_seconds,
            max_attempts=self._serial_timing.retry_limit,
            logger=logger,
        )
        self._serial_flow.set_metrics_callback(state.record_serial_flow_event)
        self._serial_flow.set_pipeline_observer(state.record_serial_pipeline_event)

        self.handshake_manager = SerialHandshakeManager(
            config=config,
            state=state,
            serial_timing=self._serial_timing,
            send_frame=self._serial_flow.send,
            enqueue_mqtt=mqtt_transport.enqueue_mqtt,
            acknowledge_frame=self._serial_flow.acknowledge,
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
            send_frame=self._serial_flow.send,
            acknowledge_frame=self._serial_flow.acknowledge,
            is_topic_action_allowed=self._is_topic_action_allowed,
            reject_topic_action=self._reject_topic_action,
            publish_bridge_snapshot=self._publish_bridge_snapshot,
            on_frame_received=self._serial_flow.on_frame_received,
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

    @property
    def serial_flow(self) -> SerialFlowController:
        """Access to the serial flow controller (SIL-2)."""
        return self._serial_flow

    @property
    def mqtt_flow(self) -> Any:
        """Access to the MQTT transport."""
        return self._mqtt_transport

    def register_serial_sender(self, sender: SendFrameCallable) -> None:
        """Allow the serial transport to provide its send coroutine."""
        self._serial_flow.set_sender(sender)

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
        """Entry point invoked by the serial transport for each MCU frame."""
        await self.dispatcher.dispatch_mcu_frame(command_id, sequence_id, payload)

    async def handle_mqtt_message(self, inbound: Message) -> None:
        """Entry point invoked by the MQTT transport for each inbound message."""
        await self.dispatcher.dispatch_mqtt_message(
            inbound,
            lambda t: parse_topic(self.state.mqtt_topic_prefix, t),
        )



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
        await self.mqtt_flow.publish(
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
        await self.mqtt_flow.publish(
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
        await self.mqtt_flow.publish(
            topic=status_topic,
            payload=payload,
            content_type="application/msgpack",
            expiry=MQTT_EXPIRY_SHELL,
            properties=(("bridge-error", TOPIC_FORBIDDEN_REASON),),
            reply_to=inbound,
        )
