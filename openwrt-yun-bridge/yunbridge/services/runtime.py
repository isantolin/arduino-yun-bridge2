"""High-level service layer for the Yun Bridge daemon.

This module encapsulates the business logic that previously lived in the
monolithic bridge_daemon.py file: command handlers for MCU frames,
reactions to MQTT messages, filesystem helpers, and process management.

By concentrating the behaviour inside BridgeService we enable the
transport layer (serial and MQTT) to focus purely on moving bytes while
this service operates on validated payloads.
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import struct
from typing import Any, Awaitable, Callable, Coroutine, Optional

from yunbridge.rpc.protocol import Command, MAX_PAYLOAD_SIZE, Status

from ..config.settings import RuntimeConfig
from ..const import (
    TOPIC_ANALOG,
    TOPIC_CONSOLE,
    TOPIC_DATASTORE,
    TOPIC_DIGITAL,
    TOPIC_FILE,
    TOPIC_MAILBOX,
    TOPIC_SHELL,
    TOPIC_STATUS,
    TOPIC_SYSTEM,
)
from ..mqtt import PublishableMessage
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
from .serial_flow import SerialFlowController

SendFrameCallable = Callable[[int, bytes], Awaitable[bool]]

logger = logging.getLogger("yunbridge.service")

STATUS_VALUES = {status.value for status in Status}


class BridgeService:
    """Service façade orchestrating MCU and MQTT interactions."""

    def __init__(self, config: RuntimeConfig, state: RuntimeState) -> None:
        self.config = config
        self.state = state
        self._serial_sender: Optional[SendFrameCallable] = None
        self._background_tasks: set[asyncio.Task[None]] = set()

        self._console = ConsoleComponent(config, state, self)
        self._datastore = DatastoreComponent(config, state, self)
        self._file = FileComponent(config, state, self)
        self._mailbox = MailboxComponent(config, state, self)
        self._pin = PinComponent(config, state, self)
        self._process = ProcessComponent(config, state, self)
        self._shell = ShellComponent(config, state, self, self._process)
        self._system = SystemComponent(config, state, self)

        self._mcu_handlers: dict[
            int, Callable[[bytes], Awaitable[Optional[bool]]]
        ] = {
            Command.CMD_DIGITAL_READ_RESP.value: (
                self._pin.handle_digital_read_resp
            ),
            Command.CMD_ANALOG_READ_RESP.value: (
                self._pin.handle_analog_read_resp
            ),
            Command.CMD_XOFF.value: self._console.handle_xoff,
            Command.CMD_XON.value: self._console.handle_xon,
            Command.CMD_CONSOLE_WRITE.value: self._console.handle_write,
            Command.CMD_DATASTORE_PUT.value: self._datastore.handle_put,
            Command.CMD_DATASTORE_GET.value: (
                self._datastore.handle_get_request
            ),
            Command.CMD_DATASTORE_GET_RESP.value: (
                self._datastore.handle_get_response
            ),
            Command.CMD_MAILBOX_PUSH.value: self._mailbox.handle_push,
            Command.CMD_MAILBOX_AVAILABLE.value: (
                self._mailbox.handle_available
            ),
            Command.CMD_MAILBOX_READ.value: self._mailbox.handle_read,
            Command.CMD_MAILBOX_PROCESSED.value: (
                self._mailbox.handle_processed
            ),
            Command.CMD_FILE_WRITE.value: self._file.handle_write,
            Command.CMD_FILE_READ.value: self._file.handle_read,
            Command.CMD_FILE_REMOVE.value: self._file.handle_remove,
            Command.CMD_PROCESS_RUN.value: self._process.handle_run,
            Command.CMD_PROCESS_RUN_ASYNC.value: (
                self._process.handle_run_async
            ),
            Command.CMD_PROCESS_POLL.value: self._process.handle_poll,
            Command.CMD_PROCESS_KILL.value: self._handle_process_kill,
            Command.CMD_GET_FREE_MEMORY_RESP.value: (
                self._system.handle_get_free_memory_resp
            ),
            Command.CMD_LINK_SYNC_RESP.value: (
                self._handle_link_sync_resp
            ),
            Command.CMD_LINK_RESET_RESP.value: (
                self._handle_link_reset_resp
            ),
            Command.CMD_GET_VERSION_RESP.value: (
                self._system.handle_get_version_resp
            ),
            Status.ACK.value: self._handle_ack,
            Status.OK.value: self._status_handler(Status.OK),
            Status.ERROR.value: self._status_handler(Status.ERROR),
            Status.CMD_UNKNOWN.value: (
                self._status_handler(Status.CMD_UNKNOWN)
            ),
            Status.MALFORMED.value: self._status_handler(Status.MALFORMED),
            Status.CRC_MISMATCH.value: (
                self._status_handler(Status.CRC_MISMATCH)
            ),
            Status.TIMEOUT.value: self._status_handler(Status.TIMEOUT),
            Status.NOT_IMPLEMENTED.value: (
                self._status_handler(Status.NOT_IMPLEMENTED)
            ),
        }

        self._serial_flow = SerialFlowController(
            ack_timeout=config.serial_retry_timeout,
            response_timeout=config.serial_response_timeout,
            max_attempts=config.serial_retry_attempts,
            logger=logger,
        )

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

    def schedule_background(
        self, coroutine: Coroutine[Any, Any, None]
    ) -> None:
        task: asyncio.Task[None] = asyncio.create_task(coroutine)
        self._background_tasks.add(task)

        def _release(finished: asyncio.Future[None]) -> None:
            self._background_tasks.discard(task)
            try:
                finished.result()
            except asyncio.CancelledError:
                logger.debug("Background task cancelled")
            except Exception:
                logger.exception("Background task failed")

        task.add_done_callback(_release)

    async def on_serial_connected(self) -> None:
        """Run post-connection initialisation for the MCU link."""

        handshake_ok = False
        try:
            handshake_ok = await self.sync_link()
        except Exception:
            logger.exception("Failed to synchronise MCU link after reconnect")

        if not handshake_ok:
            logger.error(
                "Skipping post-connect initialisation because MCU link sync failed"
            )
            return

        try:
            version_ok = await self._system.request_mcu_version()
            if not version_ok:
                logger.warning(
                    "Failed to dispatch MCU version request after reconnect"
                )
        except Exception:
            logger.exception("Failed to request MCU version after reconnect")

        try:
            await self._console.flush_queue()
        except Exception:
            logger.exception(
                "Failed to flush console backlog after reconnect"
            )

    async def on_serial_disconnected(self) -> None:
        """Reset transient MCU tracking when the serial link drops."""

        pending_digital = len(self.state.pending_digital_reads)
        pending_analog = len(self.state.pending_analog_reads)
        pending_datastore = len(self.state.pending_datastore_gets)

        total_pending = pending_digital + pending_analog + pending_datastore
        if total_pending:
            logger.warning(
                "Serial link lost; clearing %d pending request(s) "
                "(digital=%d analog=%d datastore=%d)",
                total_pending,
                pending_digital,
                pending_analog,
                pending_datastore,
            )

        self.state.pending_digital_reads.clear()
        self.state.pending_analog_reads.clear()
        self.state.pending_datastore_gets.clear()

        # Ensure we do not keep the console in a paused state between links.
        self._console.on_serial_disconnected()
        await self._serial_flow.reset()

    async def handle_mcu_frame(self, command_id: int, payload: bytes) -> None:
        """Entry point invoked by the serial transport for each MCU frame."""

        self._serial_flow.on_frame_received(command_id, payload)
        try:
            await self._dispatch_mcu_frame(command_id, payload)
        except Exception:
            logger.exception(
                "Error handling MCU frame: CMD=0x%02X payload=%s",
                command_id,
                payload.hex(),
            )

    async def handle_mqtt_message(self, topic: str, payload: bytes) -> None:
        try:
            await self._dispatch_mqtt_message(topic, payload)
        except Exception:
            logger.exception(
                "Error processing MQTT message on topic %s", topic
            )

    async def enqueue_mqtt(self, message: PublishableMessage) -> None:
        queue = self.state.mqtt_publish_queue
        while True:
            try:
                queue.put_nowait(message)
                return
            except asyncio.QueueFull:
                try:
                    dropped = queue.get_nowait()
                except asyncio.QueueEmpty:
                    await asyncio.sleep(0)
                    continue

                queue.task_done()
                drop_topic = dropped.topic_name
                self.state.record_mqtt_drop(drop_topic)
                logger.warning(
                    "MQTT publish queue saturated (%d/%d); dropping oldest "
                    "topic=%s",
                    queue.qsize(),
                    self.state.mqtt_queue_limit,
                    drop_topic,
                )

    async def sync_link(self) -> bool:
        nonce = os.urandom(4)
        self.state.link_handshake_nonce = nonce
        self.state.link_is_synchronized = False
        reset_ok = await self.send_frame(Command.CMD_LINK_RESET.value, b"")
        if not reset_ok:
            logger.warning("Failed to emit LINK_RESET during handshake")
            return False
        await asyncio.sleep(0.05)
        sync_ok = await self.send_frame(Command.CMD_LINK_SYNC.value, nonce)
        if not sync_ok:
            logger.warning("Failed to emit LINK_SYNC during handshake")
            return False

        confirmed = await self._wait_for_link_sync_confirmation(nonce)
        if not confirmed:
            logger.warning("MCU link synchronisation did not confirm within timeout")
            return False
        return True

    async def _wait_for_link_sync_confirmation(self, nonce: bytes) -> bool:
        loop = asyncio.get_running_loop()
        timeout = max(0.5, self.config.serial_response_timeout)
        deadline = loop.time() + timeout
        while loop.time() < deadline:
            if (
                self.state.link_is_synchronized
                and self.state.link_handshake_nonce is None
            ):
                return True
            if self.state.link_handshake_nonce != nonce and not self.state.link_is_synchronized:
                break
            await asyncio.sleep(0.01)
        return (
            self.state.link_is_synchronized
            and self.state.link_handshake_nonce is None
        )

    def _should_acknowledge_mcu_frame(self, command_id: int) -> bool:
        return command_id not in STATUS_VALUES

    async def _acknowledge_mcu_frame(
        self,
        command_id: int,
        *,
        status: Status = Status.ACK,
        extra: bytes = b"",
    ) -> None:
        payload = struct.pack(">H", command_id)
        if extra:
            remaining = MAX_PAYLOAD_SIZE - len(payload)
            if remaining > 0:
                payload += extra[:remaining]
        await self.send_frame(status.value, payload)

    # --- MCU command handlers ---

    async def _dispatch_mcu_frame(
        self, command_id: int, payload: bytes
    ) -> None:
        handler = self._mcu_handlers.get(command_id)
        command_name: Optional[str] = None
        try:
            command_name = Command(command_id).name
        except ValueError:
            try:
                command_name = Status(command_id).name
            except ValueError:
                command_name = f"UNKNOWN_CMD_ID(0x{command_id:02X})"

        handled_successfully = False

        if handler:
            logger.debug("MCU > %s payload=%s", command_name, payload.hex())
            result = await handler(payload)
            handled_successfully = result is not False
        elif command_id < 0x80:
            logger.warning("Unhandled MCU command %s", command_name)
            await self.send_frame(Status.NOT_IMPLEMENTED.value, b"")
        else:
            logger.debug("Ignoring MCU response %s", command_name)

        if handled_successfully and self._should_acknowledge_mcu_frame(
            command_id
        ):
            await self._acknowledge_mcu_frame(command_id)

    async def _dispatch_mqtt_message(self, topic: str, payload: bytes) -> None:
        await self._handle_mqtt_topic(topic, payload)

    # ------------------------------------------------------------------
    # MCU -> Linux handlers
    # ------------------------------------------------------------------

    async def _handle_ack(self, payload: bytes) -> None:
        if len(payload) >= 2:
            command_id = int.from_bytes(payload[:2], "big")
            logger.debug("MCU > ACK received for 0x%02X", command_id)
        else:
            logger.debug("MCU > ACK received")

    def _status_handler(
        self, status: Status
    ) -> Callable[[bytes], Awaitable[None]]:
        async def _handler(payload: bytes) -> None:
            await self._handle_status(status, payload)

        return _handler

    async def _handle_status(self, status: Status, payload: bytes) -> None:
        text = payload.decode("utf-8", errors="ignore") if payload else ""
        log_method = logger.warning if status != Status.ACK else logger.debug
        log_method("MCU > %s %s", status.name, text)

        report = json.dumps(
            {
                "status": status.value,
                "name": status.name,
                "message": text,
            }
        ).encode("utf-8")
        await self.enqueue_mqtt(
            PublishableMessage(
                topic_name=(
                    f"{self.state.mqtt_topic_prefix}/"
                    f"{TOPIC_SYSTEM}/{TOPIC_STATUS}"
                ),
                payload=report,
            )
        )

    async def _handle_get_free_memory_resp(self, payload: bytes) -> None:
        if len(payload) != 2:
            logger.warning(
                "Malformed GET_FREE_MEMORY_RESP payload: %s", payload.hex()
            )
            return

        free_memory = int.from_bytes(payload, "big")
        topic = f"{self.state.mqtt_topic_prefix}/system/free_memory/value"
        message = PublishableMessage(
            topic_name=topic, payload=str(free_memory).encode("utf-8")
        )
        await self.enqueue_mqtt(message)

    async def _handle_link_sync_resp(self, payload: bytes) -> bool:
        expected = self.state.link_handshake_nonce
        if expected is None:
            logger.warning("Unexpected LINK_SYNC_RESP without pending nonce")
            await self._acknowledge_mcu_frame(
                Command.CMD_LINK_SYNC_RESP.value,
                status=Status.MALFORMED,
                extra=payload[: MAX_PAYLOAD_SIZE - 2],
            )
            return False

        if payload != expected:
            logger.warning(
                "LINK_SYNC_RESP nonce mismatch (expected %s got %s)",
                expected.hex(),
                payload.hex(),
            )
            await self._acknowledge_mcu_frame(
                Command.CMD_LINK_SYNC_RESP.value,
                status=Status.MALFORMED,
                extra=payload[: MAX_PAYLOAD_SIZE - 2],
            )
            self.state.link_is_synchronized = False
            self.state.link_handshake_nonce = None
            return False

        self.state.link_is_synchronized = True
        self.state.link_handshake_nonce = None
        logger.info("MCU link synchronised (nonce=%s)", payload.hex())
        return True

    async def _handle_link_reset_resp(self, payload: bytes) -> bool:
        logger.info("MCU link reset acknowledged (payload=%s)", payload.hex())
        self.state.link_is_synchronized = False
        return True

    async def _handle_get_version_resp(self, payload: bytes) -> None:
        if len(payload) != 2:
            logger.warning(
                "Malformed GET_VERSION_RESP payload: %s", payload.hex()
            )
            return

        major, minor = payload[0], payload[1]
        self.state.mcu_version = (major, minor)
        topic = f"{self.state.mqtt_topic_prefix}/system/version/value"
        message = PublishableMessage(
            topic_name=topic,
            payload=f"{major}.{minor}".encode("utf-8"),
        )
        await self.enqueue_mqtt(message)
        logger.info("MCU firmware version reported as %d.%d", major, minor)

    # ------------------------------------------------------------------
    # Process management
    # ------------------------------------------------------------------

    def is_command_allowed(self, command: str) -> bool:
        return self.state.allowed_policy.is_allowed(command)

    async def _run_command_sync(
        self, command: str
    ) -> tuple[int, bytes, bytes, Optional[int]]:
        return await self._process.run_sync(command)

    async def _collect_process_output(
        self, pid: int
    ) -> tuple[int, int, bytes, bytes, bool, bool, bool]:
        return await self._process.collect_output(pid)

    def _trim_process_buffers(
        self, stdout_buffer: bytearray, stderr_buffer: bytearray
    ) -> tuple[bytes, bytes, bool, bool]:
        return self._process.trim_buffers(stdout_buffer, stderr_buffer)

    async def _handle_process_kill(
        self, payload: bytes, *, send_ack: bool = True
    ) -> bool:
        return await self._process.handle_kill(payload, send_ack=send_ack)

    # ------------------------------------------------------------------
    # MQTT topic handling
    # ------------------------------------------------------------------

    async def _handle_mqtt_topic(self, topic: str, payload: bytes) -> None:
        parts = topic.split("/")
        if not parts or parts[0] != self.state.mqtt_topic_prefix:
            logger.debug(
                "Ignoring MQTT message with unexpected prefix: %s", topic
            )
            return

        if len(parts) < 2:
            logger.debug("MQTT topic missing type segment: %s", topic)
            return

        payload_str = payload.decode("utf-8", errors="ignore")
        topic_type = parts[1]
        identifier = parts[2] if len(parts) >= 3 else ""

        try:
            if topic_type == TOPIC_FILE and len(parts) >= 4:
                await self._file.handle_mqtt(identifier, parts[3:], payload)
            elif topic_type == TOPIC_CONSOLE and identifier == "in":
                await self._console.handle_mqtt_input(payload)
            elif topic_type == TOPIC_DATASTORE and len(parts) >= 3:
                await self._datastore.handle_mqtt(
                    identifier,
                    parts[3:],
                    payload,
                    payload_str,
                )
            elif topic_type == TOPIC_MAILBOX and identifier == "write":
                await self._mailbox.handle_mqtt_write(payload)
            elif topic_type == TOPIC_MAILBOX and identifier == "read":
                await self._mailbox.handle_mqtt_read()
            elif topic_type == TOPIC_SHELL:
                await self._shell.handle_mqtt(parts, payload_str)
            elif topic_type in (TOPIC_DIGITAL, TOPIC_ANALOG):
                await self._pin.handle_mqtt(topic_type, parts, payload_str)
            elif topic_type == TOPIC_SYSTEM:
                handled = await self._system.handle_mqtt(
                    identifier,
                    parts[3:] if len(parts) > 3 else [],
                )
                if not handled:
                    logger.debug("Unhandled MQTT system topic %s", topic)
            else:
                logger.debug("Unhandled MQTT topic %s", topic)
        except Exception:
            logger.exception("Error processing MQTT topic: %s", topic)
