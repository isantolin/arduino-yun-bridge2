from __future__ import annotations

import asyncio
import structlog
from collections.abc import Coroutine
from typing import TYPE_CHECKING, Any

import msgspec
import svcs
from aiomqtt.message import Message

from ..config.settings import RuntimeConfig
from ..protocol.protocol import Status
from ..protocol.structures import AckPacket
from ..protocol.topics import parse_topic
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
    SerialHandshakeFatal,
    SerialHandshakeManager,
    SerialTimingWindow,
    derive_serial_timing,
)
from .serial_flow import SerialFlowController

logger = structlog.get_logger("mcubridge.service")


STATUS_VALUES = {status.value for status in Status}


class BridgeService:
    """Service façade orchestrating MCU and MQTT interactions (SIL-2)."""

    def __init__(self, config: RuntimeConfig, state: RuntimeState, mqtt_transport: Any) -> None:
        self.config = config
        self.state = state
        self._mqtt_transport = mqtt_transport
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
            self._registry.register_factory(
                comp_cls,
                lambda c=comp_cls: c(config, state, self),
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
            is_topic_action_allowed=mqtt_transport.is_topic_action_allowed,
            reject_topic_action=mqtt_transport.reject_topic_action,
            publish_bridge_snapshot=mqtt_transport.publish_bridge_snapshot,
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
        return self._serial_flow

    @property
    def mqtt_flow(self) -> Any:
        return self._mqtt_transport

    def register_serial_sender(self, sender: SendFrameCallable) -> None:
        self._serial_flow.set_sender(sender)

    async def schedule_background(
        self,
        coroutine: Coroutine[Any, Any, None],
        *,
        name: str | None = None,
    ) -> asyncio.Task[Any]:
        if not self._task_group:
            raise RuntimeError("BridgeService context not entered")
        return self._task_group.create_task(coroutine, name=name)

    async def on_serial_connected(self) -> None:
        self.state.mark_transport_connected()
        try:
            await self.handshake_manager.synchronize()
        except SerialHandshakeFatal:
            raise
        except (OSError, ValueError, RuntimeError) as e:
            logger.exception("Failed to synchronize link after reconnect: %s", e)

        if not self.state.is_synchronized:
            self.handshake_manager.raise_if_handshake_fatal()
            return

        try:
            await self._container.get(SystemComponent).request_mcu_version()
            await self._container.get(ConsoleComponent).flush_queue()
        except (OSError, ValueError, RuntimeError) as e:
            logger.exception("Post-connection initialization failed: %s", e)

    async def on_serial_disconnected(self) -> None:
        self.state.mark_transport_disconnected()
        self.state.pending_digital_reads.clear()
        self.state.pending_analog_reads.clear()
        self._container.get(ConsoleComponent).on_serial_disconnected()
        await self._serial_flow.reset()
        self.handshake_manager.clear_handshake_expectations()

    async def handle_mcu_frame(
        self, command_id: int, sequence_id: int, payload: bytes
    ) -> None:
        try:
            await self.dispatcher.dispatch_mcu_frame(command_id, sequence_id, payload)
        except (ValueError, TypeError, RuntimeError, OSError) as e:
            logger.error("MCU > Dispatch error cmd=0x%02X: %s", command_id, e)

    async def handle_mqtt_message(self, inbound: Message) -> None:
        try:
            await self.dispatcher.dispatch_mqtt_message(
                inbound,
                lambda t: parse_topic(self.state.mqtt_topic_prefix, t),
            )
        except (ValueError, TypeError, RuntimeError, OSError) as e:
            logger.error("MQTT > Dispatch error topic=%s: %s", str(inbound.topic), e)

    async def _handle_ack(self, seq_id: int, payload: bytes) -> None:
        if len(payload) >= 2:
            try:
                packet = AckPacket.decode(payload)
                logger.debug("MCU > ACK received for 0x%02X", packet.command_id)
            except (msgspec.ValidationError, ValueError) as exc:
                logger.warning("MCU > Malformed ACK payload: %s", exc)
        else:
            logger.debug("MCU > ACK received")

    async def handle_status(self, seq_id: int, status: Status, payload: bytes) -> None:
        self.state.record_mcu_status(status)
        text = payload.decode("utf-8", errors="ignore") if payload else ""
        log_method = logger.warning if status not in {Status.OK, Status.ACK} else logger.debug
        log_method("MCU > %s (seq=%d): %s %s", status.name, seq_id, status.description, text)

        from mcubridge.config.const import MQTT_EXPIRY_SHELL
        from mcubridge.protocol.topics import topic_path, Topic

        status_topic = topic_path(self.state.mqtt_topic_prefix, Topic.SYSTEM, Topic.STATUS)
        report = msgspec.msgpack.encode({
            "status": status.value, "name": status.name,
            "description": status.description, "message": text
        })
        props = [("bridge-status", status.name), ("bridge-status-description", status.description)]
        if text: props.append(("bridge-status-message", text))

        await self.mqtt_flow.publish(
            topic=status_topic, payload=report, content_type="application/msgpack",
            expiry=MQTT_EXPIRY_SHELL, properties=tuple(props)
        )
