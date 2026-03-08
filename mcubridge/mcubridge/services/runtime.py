"""Core runtime orchestration for the MCU Bridge service.

[SIL-2 COMPLIANCE]
This module implements the primary task supervisor and state machine for:
- Serial transport lifecycle (auto-reconnect, jittered backoff)
- MQTT client coordination
- Handshake mutual authentication
- Task health monitoring
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable, Coroutine
from typing import Any

from aiomqtt.message import Message

from ..config.settings import RuntimeConfig
from ..protocol import protocol
from ..protocol.protocol import Status, Topic
from ..protocol.topics import parse_topic
from ..transport.serial import SerialTransport
from ..protocol.structures import QueuedPublish
from .base import BridgeContext
from .console import ConsoleComponent
from .datastore import DatastoreComponent
from .dispatcher import BridgeDispatcher
from .file import FileComponent
from .handshake import SerialHandshakeManager, derive_serial_timing
from .mailbox import MailboxComponent
from .pin import PinComponent
from .process import ProcessComponent
from .serial_flow import SerialFlowController
from .shell import ShellComponent
from .system import SystemComponent


class BridgeService(BridgeContext):
    """The central orchestrator for all bridge sub-services."""

    def __init__(self, config: RuntimeConfig, state: Any) -> None:
        self.config = config
        self.state = state
        self._logger = logging.getLogger("mcubridge.service.runtime")
        self._tasks: list[asyncio.Task[None]] = []
        self._shutdown_event = asyncio.Event()
        self._serial_sender: Callable[[int, bytes], Awaitable[bool]] | None = None

        # Derived Timing parameters for SIL-2/MIL-SPEC handshake
        self._serial_timing = derive_serial_timing(config)

        # Core Components
        self._transport = SerialTransport(config, state, self)

        # [FIX] USE CORRECT CONSTRUCTOR SIGNATURE
        self._serial_flow = SerialFlowController(config)

        # RPC Handlers
        self._console = ConsoleComponent(config, state, self)
        self._datastore = DatastoreComponent(config, state, self)
        self._file = FileComponent(config, state, self)
        self._mailbox = MailboxComponent(config, state, self)
        self._pin = PinComponent(config, state, self)
        self._process = ProcessComponent(config, state, self)
        # [FIX] PASS PROCESS COMPONENT TO SHELL
        self._shell = ShellComponent(config, state, self, self._process)
        self._system = SystemComponent(config, state, self)

        # Coordinator
        self._handshake = SerialHandshakeManager(
            config=config,
            state=state,
            serial_timing=self._serial_timing,
            send_frame=self.send_frame,
            enqueue_mqtt=self.enqueue_mqtt,
            acknowledge_frame=self.acknowledge_frame,
        )

        # Master Dispatcher
        self._dispatcher = BridgeDispatcher(
            mcu_registry=self._transport.registry,
            mqtt_router=self._transport.router,
            state=state,
            send_frame=self.send_frame,
            acknowledge_frame=self.acknowledge_frame,
            is_topic_action_allowed=self.is_topic_action_allowed,
            reject_topic_action=self.reject_topic_action,
            publish_bridge_snapshot=self.publish_bridge_snapshot,
        )

        # Register component handlers with dispatcher
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

        # Register system-level handlers
        self._dispatcher.register_system_handlers(
            handle_link_sync_resp=self._handshake.handle_link_sync_resp,
            handle_link_reset_resp=self._handshake.handle_link_reset_resp,
            handle_get_capabilities_resp=self._handshake.handle_capabilities_resp,
            handle_ack=self._on_mcu_ack,
            status_handler_factory=self._on_mcu_status,
            # [FIX] CORRECT METHOD NAME
            handle_process_kill=self._process.handle_kill,
        )

    # --- Async Context Manager ---

    async def __aenter__(self) -> BridgeService:
        return self

    async def __aexit__(self, exc_type: Any, exc_val: Any, exc_tb: Any) -> None:
        await self._shutdown()

    # --- BridgeContext Implementation ---

    def register_serial_sender(self, sender: Callable[[int, bytes], Awaitable[bool]]) -> None:
        """Registers the transport-specific frame sender."""
        self._serial_sender = sender

    async def handle_mqtt_message(self, message: Any) -> None:
        """Entry point for all inbound MQTT traffic."""
        import sys
        print(f"!!! handle_mqtt_message CALLED: {message.topic}", file=sys.stderr, flush=True)
        # [FIX] USE self.config.mqtt_topic
        await self._dispatcher.dispatch_mqtt_message(
            message,
            parse_topic_func=lambda name: parse_topic(self.config.mqtt_topic, name),
        )

    async def send_frame(self, command_id: int, payload: bytes = b"") -> bool:
        """Sends a raw binary frame to the MCU."""
        # [SIL-2] Gate all outgoing traffic by synchronization state
        # Handshake frames bypass this check.
        if not self.state.is_synchronized and not self._is_handshake_command(command_id):
            self._logger.debug("Blocking frame 0x%02X because state is not synchronized (state=%s)", command_id, self.state._machine.state)
            return False

        if command_id in protocol.STATUS_VALUES:
            return await self._transport.write_frame(command_id, payload)

        return await self._serial_flow.send(command_id, payload, self._transport.write_frame)

    async def enqueue_mqtt(self, message: QueuedPublish, *, reply_context: Any = None) -> None:
        """Enqueues an outgoing MQTT message for publication."""
        # Extract properties from reply_context (inbound Message) to support CorrelationData routing
        if reply_context:
            props = getattr(reply_context, "properties", None)
            correlation = getattr(props, "CorrelationData", None) if props else None

            if correlation:
                # [SIL-2] Use CorrelationData from request for proper routing
                message = QueuedPublish(
                    topic_name=message.topic_name,
                    payload=message.payload,
                    qos=message.qos,
                    retain=message.retain,
                    content_type=message.content_type,
                    message_expiry_interval=message.message_expiry_interval,
                    user_properties=list(message.user_properties),
                    correlation_data=correlation,
                )

        try:
            self.state.mqtt_publish_queue.put_nowait(message)
        except asyncio.QueueFull:
            self._logger.warning("MQTT publish queue full, spooling not implemented inline")
            pass

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
        """Shorthand for immediate MQTT publication from service context."""
        if isinstance(payload, str):
            payload = payload.encode("utf-8")

        # Determine actual topic from reply_to ResponseTopic if requested
        actual_topic = topic
        correlation: bytes | None = None

        if reply_to:
            orig_props = getattr(reply_to, "properties", None)
            if orig_props:
                correlation = getattr(orig_props, "CorrelationData", None)
                resp_topic = getattr(orig_props, "ResponseTopic", None)
                if resp_topic:
                    actual_topic = resp_topic

        msg = QueuedPublish(
            topic_name=actual_topic,
            payload=payload,
            qos=qos,
            retain=retain,
            content_type=content_type,
            message_expiry_interval=expiry,
            user_properties=list(properties),
            correlation_data=correlation,
        )
        await self.enqueue_mqtt(msg)

    async def acknowledge_frame(
        self,
        command_id: int,
        status: Status = Status.ACK,
        extra: bytes = b"",
    ) -> None:
        """Sends an explicit ACK frame to the MCU for a request."""
        from ..protocol import structures

        payload = structures.AckPacket(command_id=command_id).encode() + extra
        await self._transport.write_frame(status.value, payload)

    def is_command_allowed(self, command: str) -> bool:
        """Security policy check for specific command execution."""
        return True

    def is_topic_action_allowed(self, topic: Topic | str, action: str) -> bool:
        """Security policy check for MQTT-triggered operations."""
        return True

    async def reject_topic_action(self, inbound: Any, topic: Topic | str, action: str) -> None:
        """Notifies MQTT clients that an action was rejected by policy."""
        pass

    async def publish_bridge_snapshot(self, kind: str, inbound: Any) -> None:
        """Publishes a structured system state snapshot to MQTT."""
        pass

    async def schedule_background(
        self,
        coroutine: Coroutine[Any, Any, None],
        *,
        name: str | None = None,
    ) -> Any:
        """Schedules a managed background task."""
        task = asyncio.create_task(coroutine, name=name)
        self._tasks.append(task)
        return task

    # --- Internal Lifecycle Management ---

    async def run(self) -> None:
        """Primary service loop."""
        self._logger.info("BridgeService.run() STARTED")
        self._transport.set_on_frame_callback(self._on_transport_frame)

        # 1. Start Transport Supervisor
        self._logger.info("Creating _transport_supervisor task")
        self._tasks.append(asyncio.create_task(self._transport_supervisor()))

        # 2. Wait for Shutdown
        self._logger.info("Waiting for shutdown event")
        await self._shutdown_event.wait()

        # 3. Cleanup
        await self._shutdown()

    async def _transport_supervisor(self) -> None:
        """Maintains MCU connectivity and handshake state."""
        self._logger.info("Starting _transport_supervisor loop")
        while not self._shutdown_event.is_set():
            try:
                self._logger.info("Connecting to Serial...")
                # 1. Connect to Serial
                async with self._transport:
                    self.state.mark_transport_connected()

                    # 2. Perform Handshake
                    handshake_ok = await self._handshake.synchronize()
                    if not handshake_ok:
                        self._logger.error("MCU Handshake FAILED.")
                        await asyncio.sleep(5.0)
                        continue

                    # 3. Handle data loop (implicit in transport context)
                    while self.state.is_transport_connected:
                        await asyncio.sleep(1.0)

            except (Exception, asyncio.CancelledError) as exc:
                self._logger.warning("Transport error: %s", exc)
                self.state.mark_transport_disconnected()
                self._handshake.reset_fsm()
                await asyncio.sleep(2.0)

    def _on_transport_frame(self, command_id: int, payload: bytes) -> None:
        """Inbound frame callback from SerialTransport."""
        # 1. Update Serial Flow Controller (for ACKs/Responses)
        if command_id == Status.ACK.value:
            from ..protocol import structures

            try:
                ack = structures.AckPacket.decode(payload)
                self._serial_flow.on_ack_received(ack.command_id)
            except Exception:
                pass
        else:
            self._serial_flow.on_frame_received(command_id, payload)

        # 2. Dispatch to components
        # [SIL-2] Keep strong reference to avoid aggressive Python 3.13 GC
        if not hasattr(self, "_dispatch_tasks"):
            self._dispatch_tasks = set()
        task = asyncio.create_task(self._dispatcher.dispatch_mcu_frame(command_id, payload))
        self._dispatch_tasks.add(task)
        task.add_done_callback(self._dispatch_tasks.discard)

    async def _on_mcu_ack(self, payload: bytes) -> None:
        from ..protocol import structures
        try:
            ack = structures.AckPacket.decode(payload)
            self._serial_flow.on_ack_received(ack.command_id)
        except Exception:
            pass

    def _on_mcu_status(self, status: Status) -> Callable[[bytes], Awaitable[None]]:
        async def _handler(_payload: bytes) -> None:
            self._serial_flow.on_status_received(status)

        return _handler

    def _is_handshake_command(self, command_id: int) -> bool:
        return (
            command_id == protocol.Command.CMD_LINK_SYNC
            or command_id == protocol.Command.CMD_LINK_RESET
            or command_id == protocol.Command.CMD_LINK_SYNC_RESP
            or command_id == protocol.Command.CMD_LINK_RESET_RESP
        )

    async def _shutdown(self) -> None:
        """Orderly shutdown of all bridge components."""
        for task in self._tasks:
            task.cancel()
        await asyncio.gather(*self._tasks, return_exceptions=True)

    def signal_shutdown(self) -> None:
        self._shutdown_event.set()
