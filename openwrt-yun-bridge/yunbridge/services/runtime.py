"""Runtime service orchestration for the Yun Bridge."""

from __future__ import annotations

import asyncio
import logging
import signal
from collections.abc import Awaitable, Callable
from typing import TYPE_CHECKING, NoReturn, cast

from yunbridge.config.settings import RuntimeConfig
from yunbridge.protocol import Topic
from yunbridge.rpc.protocol import Status
from yunbridge.services.components import (
    ConsoleComponent,
    DatastoreComponent,
    FileComponent,
    MailboxComponent,
    PinComponent,
    ProcessComponent,
    ShellComponent,
    SystemComponent,
)
from yunbridge.services.dispatcher import BridgeDispatcher
from yunbridge.services.handshake import (
    SerialHandshakeFatal,
    attempt_handshake,
    derive_serial_timing,
    handle_sync_response,
)
from yunbridge.services.routers import MCUHandlerRegistry, MQTTRouter
from yunbridge.services.serial_flow import SerialFlowControl, SerialTimingWindow
from yunbridge.services.task_supervisor import supervise_task
from yunbridge.state.context import RuntimeState
from yunbridge.transport.mqtt import build_mqtt_tls_context, mqtt_task
from yunbridge.transport.serial import SerialTransport
from yunbridge.watchdog import Watchdog

if TYPE_CHECKING:
    from aiomqtt import Message as MQTTMessage
    
    # Import necessary for type checking internal queues
    from yunbridge.state.queues import QueuedPublish

logger = logging.getLogger("yunbridge.service")

SendFrameCallable = Callable[[int, bytes], Awaitable[bool]]


class BridgeService:
    """Core service orchestrating transport, protocol dispatch, and components."""

    def __init__(self, config: RuntimeConfig, state: RuntimeState) -> None:
        self.config = config
        self.state = state
        self._serial_sender: SendFrameCallable | None = None
        self._serial_timing: SerialTimingWindow = derive_serial_timing(config)
        self._task_group: asyncio.TaskGroup | None = None

        # Instantiate components
        self._console = ConsoleComponent(config, state, self)
        self._datastore = DatastoreComponent(config, state, self)
        
        self._file = FileComponent(
            root_path=config.file_system_root,
            send_frame=self.send_frame,
            publish_mqtt=self.publish_mqtt,
            write_max_bytes=config.file_write_max_bytes,
            storage_quota_bytes=config.file_storage_quota_bytes,
        )
        
        self._mailbox = MailboxComponent(config, state, self)
        self._pin = PinComponent(config, state, self)
        self._process = ProcessComponent(config, state, self)
        self._shell = ShellComponent(config, state, self)
        self._system = SystemComponent(config, state, self)

        self._mcu_registry = MCUHandlerRegistry()
        self._mqtt_router = MQTTRouter(state.mqtt_topic_prefix)

        self._dispatcher = BridgeDispatcher(
            mcu_registry=self._mcu_registry,
            mqtt_router=self._mqtt_router,
            send_frame=self.send_frame,
            acknowledge_frame=self._acknowledge_mcu_frame,
            is_link_synchronized=lambda: self.state.link_is_synchronized,
            is_topic_action_allowed=self._is_topic_action_allowed,
            reject_topic_action=self._reject_mqtt_action,
            publish_bridge_snapshot=self._publish_bridge_snapshot,
        )

        self._register_handlers()

        self._serial_flow: SerialFlowControl = SerialFlowControl(
            state=state,
            send_raw=self._send_serial_raw,
            timing=self._serial_timing,
        )

        if config.watchdog_enabled:
            self._watchdog: Watchdog | None = Watchdog(
                interval=config.watchdog_interval,
                state=state,
            )
        else:
            self._watchdog = None

    # -- BridgeContext Protocol Implementation --

    def is_command_allowed(self, command: str) -> bool:
        """Check if a shell command is allowed by policy."""
        return self.config.allowed_policy.is_allowed(command)

    def schedule_background(self, coro: Awaitable[object]) -> None:
        """Schedule a background task within the service's task group."""
        if self._task_group:
            self._task_group.create_task(coro) # type: ignore
        else:
            logger.error("Cannot schedule background task: TaskGroup not active")

    # -- End Protocol Implementation --

    def _register_handlers(self) -> None:
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
            handle_link_sync_resp=self._handle_link_sync_resp,
            handle_link_reset_resp=self._handle_link_reset_resp,
            handle_ack=self._handle_ack,
            status_handler_factory=self._make_status_handler,
            handle_process_kill=self._process.handle_kill,
        )

    def register_serial_sender(self, sender: SendFrameCallable) -> None:
        self._serial_sender = sender

    async def start(self) -> NoReturn:
        logger.info("Yun Bridge service starting...")
        
        loop = asyncio.get_running_loop()
        stop_event = asyncio.Event()

        def _signal_handler(sig_name: str) -> None:
            logger.info("Received signal %s; stopping...", sig_name)
            stop_event.set()

        for sig in (signal.SIGINT, signal.SIGTERM):
            loop.add_signal_handler(sig, lambda: _signal_handler(sig.name))

        transport = SerialTransport(self.config, self.state, self)
        
        mqtt_tls = build_mqtt_tls_context(self.config)

        async with asyncio.TaskGroup() as tg:
            self._task_group = tg
            
            tg.create_task(
                supervise_task(transport.run, "serial_transport", self.state) # type: ignore
            )
            
            tg.create_task(
                supervise_task(
                    lambda: mqtt_task(self.config, self.state, self, mqtt_tls),
                    "mqtt_transport",
                    self.state,
                )
            )

            if self._watchdog:
                tg.create_task(
                    supervise_task(self._watchdog.run, "watchdog", self.state) # type: ignore
                )

            await stop_event.wait()
            logger.info("Stopping services...")
            raise asyncio.CancelledError()

    async def on_serial_connected(self) -> None:
        self.state.serial_link_connected = True
        self._serial_flow.reset()
        logger.info("Serial link established. Starting handshake...")
        try:
            await attempt_handshake(
                self.config,
                self.state,
                self.send_frame,
                self._compute_handshake_tag,
            )
            await self._on_handshake_complete()
        except SerialHandshakeFatal:
            self.state.serial_link_connected = False
            raise
        except Exception:
            self.state.serial_link_connected = False
            raise

    async def on_serial_disconnected(self) -> None:
        if self.state.serial_link_connected:
            logger.warning("Serial link lost.")
        self.state.serial_link_connected = False
        self.state.link_is_synchronized = False
        
        self.state.clear_pending_requests()
        self._serial_flow.reset()
        self.state.mcu_is_paused = False

    async def _on_handshake_complete(self) -> None:
        logger.info("Link synchronized. Ready for commands.")
        
        self._serial_flow.reset()
        self.state.mcu_is_paused = False
        
        await self._console.flush_pending_output()

    async def send_frame(self, command_id: int, payload: bytes = b"") -> bool:
        return await self._serial_flow.send(command_id, payload)

    async def handle_mcu_frame(self, command_id: int, payload: bytes) -> None:
        self._serial_flow.on_frame_received(command_id, payload)
        await self._dispatcher.dispatch_mcu_frame(command_id, payload)

    async def handle_mqtt_message(self, message: object) -> None:
        # Runtime check for type to avoid circular import issues at runtime
        # but allow strict type checking with Pylance if imports were available
        if hasattr(message, "topic") and hasattr(message, "payload"):
             # from aiomqtt import Message as MQTTMessage  # pyright: ignore
             await self._dispatcher.dispatch_mqtt_message(
                 message,  # type: ignore
                 self.state.parse_mqtt_topic
             )

    async def publish_mqtt(
        self,
        topic_suffix: str,
        payload: bytes | str,
        retain: bool = False,
    ) -> None:
        await self.state.enqueue_mqtt_publish(topic_suffix, payload, retain)
        
    # Helper to satisfy Component protocol for enqueue_mqtt
    async def enqueue_mqtt(self, message: QueuedPublish, *, reply_context: MQTTMessage | None = None) -> None:
        await self.state.mqtt_publish_queue.put(message)

    async def _acknowledge_mcu_frame(
        self,
        command_id: int,
        status: Status = Status.ACK,
        extra: bytes = b"",
    ) -> None:
        if self._serial_sender:
            import struct
            from yunbridge.rpc import protocol
            payload = struct.pack(protocol.UINT16_FORMAT, command_id) + extra
            await self._serial_sender(status.value, payload)

    async def _send_serial_raw(self, command_id: int, payload: bytes) -> bool:
        if self._serial_sender:
            return await self._serial_sender(command_id, payload)
        return False

    def _compute_handshake_tag(self, nonce: bytes) -> bytes:
        import hmac
        import hashlib
        return hmac.new(
            self.config.serial_shared_secret,
            nonce,
            hashlib.sha256,
        ).digest()[:16]

    def _is_topic_action_allowed(self, topic: Topic | str, action: str) -> bool:
        return self.config.topic_authorization.is_allowed(str(topic), action)

    async def _reject_mqtt_action(
        self,
        message: object,
        topic_enum: Topic | str,
        action: str,
    ) -> None:
        topic_str = str(topic_enum)
        msg_topic = str(getattr(message, "topic", ""))
        logger.warning(
            "Blocked MQTT action topic=%s action=%s (message topic=%s)",
            topic_str,
            action,
            msg_topic,
        )
        await self.publish_mqtt(
            f"{Topic.SYSTEM}/status",
            f'{{"status":"forbidden","topic":"{topic_str}","action":"{action}"}}',
            retain=False,
        )

    async def _publish_bridge_snapshot(self, category: str, request: object) -> None:
        import json
        snapshot = self.state.get_snapshot()
        
        payload: dict[str, object] = {}
        if category == "handshake":
            payload = cast(dict[str, object], snapshot.get("handshake", {}))
        elif category == "summary":
            payload = cast(dict[str, object], snapshot)
            
        if payload:
            await self.publish_mqtt(
                f"{Topic.SYSTEM}/bridge/{category}/value",
                json.dumps(payload),
                retain=False,
            )

    async def _handle_link_sync_resp(self, payload: bytes) -> bool:
        return await handle_sync_response(self.config, self.state, payload)

    async def _handle_link_reset_resp(self, payload: bytes) -> bool:
        logger.info("MCU link reset acknowledged (payload=%s)", payload.hex())
        return True
    
    async def _handle_ack(self, payload: bytes) -> None:
         pass # Handled by SerialFlowControl

    def _make_status_handler(self, status: Status) -> Callable[[bytes], Awaitable[None]]:
        async def _handler(payload: bytes) -> None:
             logger.warning(
                "MCU > %s %s", 
                status.name, 
                payload.decode("utf-8", errors="replace")
             )
             if status == Status.MALFORMED:
                 self.state.record_serial_decode_error()
        return _handler
        
    # Helpers exposed for testing
    async def sync_link(self) -> bool:
        try:
             await attempt_handshake(
                self.config,
                self.state,
                self.send_frame,
                self._compute_handshake_tag,
            )
             return True
        except Exception:
             return False
