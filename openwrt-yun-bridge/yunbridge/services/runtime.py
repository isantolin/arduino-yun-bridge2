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
import hashlib
import hmac
import json
import logging
import os
import time
from typing import Any, Awaitable, Callable, Coroutine, Optional

from yunbridge.rpc.protocol import Command, MAX_PAYLOAD_SIZE, Status

from ..config.settings import RuntimeConfig
from ..common import pack_u16
from ..const import (
    SERIAL_HANDSHAKE_BACKOFF_BASE,
    SERIAL_HANDSHAKE_BACKOFF_MAX,
    SERIAL_HANDSHAKE_TAG_LEN,
    SERIAL_NONCE_LENGTH,
)
from ..protocol.topics import (
    Topic,
    handshake_topic,
    parse_topic,
    topic_path,
)
from ..mqtt import InboundMessage, PublishableMessage
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
from .task_supervisor import TaskSupervisor


class SerialHandshakeFatal(RuntimeError):
    """Raised when MCU rejects the serial shared secret permanently."""


_FATAL_HANDSHAKE_REASONS: frozenset[str] = frozenset({"sync_auth_mismatch"})

SendFrameCallable = Callable[[int, bytes], Awaitable[bool]]

logger = logging.getLogger("yunbridge.service")

STATUS_VALUES = {status.value for status in Status}
_PRE_SYNC_ALLOWED_COMMANDS = {
    Command.CMD_LINK_SYNC_RESP.value,
    Command.CMD_LINK_RESET_RESP.value,
}
_TOPIC_FORBIDDEN_REASON = "topic-action-forbidden"


class BridgeService:
    """Service façade orchestrating MCU and MQTT interactions."""

    def __init__(self, config: RuntimeConfig, state: RuntimeState) -> None:
        self.config = config
        self.state = state
        self._serial_sender: Optional[SendFrameCallable] = None
        self._task_supervisor = TaskSupervisor(logger=logger)

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
            Command.CMD_DIGITAL_READ.value: (
                lambda payload, cmd=Command.CMD_DIGITAL_READ: (
                    self._pin.handle_unexpected_mcu_request(cmd, payload)
                )
            ),
            Command.CMD_ANALOG_READ.value: (
                lambda payload, cmd=Command.CMD_ANALOG_READ: (
                    self._pin.handle_unexpected_mcu_request(cmd, payload)
                )
            ),
            Command.CMD_XOFF.value: self._console.handle_xoff,
            Command.CMD_XON.value: self._console.handle_xon,
            Command.CMD_CONSOLE_WRITE.value: self._console.handle_write,
            Command.CMD_DATASTORE_PUT.value: self._datastore.handle_put,
            Command.CMD_DATASTORE_GET.value: (
                self._datastore.handle_get_request
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
        self._serial_flow.set_metrics_callback(state.record_serial_flow_event)

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
        self,
        coroutine: Coroutine[Any, Any, None],
        *,
        name: Optional[str] = None,
    ) -> asyncio.Task[Any]:
        """Schedule *coroutine* under the supervisor."""

        return self._task_supervisor.start(coroutine, name=name)

    async def cancel_background_tasks(self) -> None:
        await self._task_supervisor.cancel()

    async def on_serial_connected(self) -> None:
        """Run post-connection initialisation for the MCU link."""

        handshake_ok = False
        fatal_error: Optional[SerialHandshakeFatal] = None
        try:
            handshake_ok = await self.sync_link()
        except SerialHandshakeFatal as exc:
            fatal_error = exc
            handshake_ok = False
        except Exception:
            logger.exception("Failed to synchronise MCU link after reconnect")

        if fatal_error is not None:
            raise fatal_error

        if not handshake_ok:
            self._raise_if_handshake_fatal()
            logger.error(
                (
                    "Skipping post-connect initialisation because MCU link "
                    "sync failed"
                )
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
        self._console.on_serial_disconnected()
        await self._serial_flow.reset()
        self._clear_handshake_expectations()

    async def handle_mcu_frame(self, command_id: int, payload: bytes) -> None:
        """Entry point invoked by the serial transport for each MCU frame."""

        if not self._is_frame_allowed_pre_sync(command_id):
            logger.warning(
                "Rejecting MCU frame 0x%02X before link synchronisation",
                command_id,
            )
            if command_id < 0x80:
                await self._acknowledge_mcu_frame(
                    command_id,
                    status=Status.MALFORMED,
                    extra=payload[: MAX_PAYLOAD_SIZE - 2],
                )
            return

        self._serial_flow.on_frame_received(command_id, payload)
        try:
            await self._dispatch_mcu_frame(command_id, payload)
        except Exception:
            logger.exception(
                "Error handling MCU frame: CMD=0x%02X payload=%s",
                command_id,
                payload.hex(),
            )

    async def handle_mqtt_message(self, inbound: InboundMessage) -> None:
        try:
            await self._dispatch_mqtt_message(inbound)
        except Exception:
            logger.exception(
                "Error processing MQTT message on topic %s",
                inbound.topic_name,
            )

    async def enqueue_mqtt(
        self,
        message: PublishableMessage,
        *,
        reply_context: Optional[InboundMessage] = None,
    ) -> None:
        queue = self.state.mqtt_publish_queue
        message_to_queue = message
        if reply_context is not None:
            target_topic = (
                reply_context.response_topic
                if reply_context.response_topic
                else message.topic_name
            )
            message_to_queue = message_to_queue.with_topic(target_topic)
            if reply_context.correlation_data is not None:
                message_to_queue = message_to_queue.with_correlation_data(
                    reply_context.correlation_data
                )
            message_to_queue = message_to_queue.with_user_property(
                "bridge-request-topic",
                reply_context.topic_name,
            )

        while True:
            try:
                queue.put_nowait(message_to_queue)
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
                await self.state.stash_mqtt_message(dropped)
                logger.warning(
                    "MQTT publish queue saturated (%d/%d); dropping oldest "
                    "topic=%s",
                    queue.qsize(),
                    self.state.mqtt_queue_limit,
                    drop_topic,
                )

    async def sync_link(self) -> bool:
        await self._respect_handshake_backoff()
        nonce_length = SERIAL_NONCE_LENGTH
        self.state.record_handshake_attempt()
        nonce = os.urandom(nonce_length)
        self.state.link_handshake_nonce = nonce
        self.state.link_nonce_length = nonce_length
        self.state.link_expected_tag = (
            self._compute_handshake_tag(nonce)
        )
        self.state.link_is_synchronized = False
        reset_ok = await self.send_frame(Command.CMD_LINK_RESET.value, b"")
        if not reset_ok:
            logger.warning("Failed to emit LINK_RESET during handshake")
            self.state.link_expected_tag = None
            self.state.link_handshake_nonce = None
            self.state.link_nonce_length = 0
            await self._handle_handshake_failure("link_reset_send_failed")
            return False
        await asyncio.sleep(0.05)
        sync_ok = await self.send_frame(Command.CMD_LINK_SYNC.value, nonce)
        if not sync_ok:
            logger.warning("Failed to emit LINK_SYNC during handshake")
            self.state.link_expected_tag = None
            self.state.link_handshake_nonce = None
            self.state.link_nonce_length = 0
            await self._handle_handshake_failure("link_sync_send_failed")
            return False

        confirmed = await self._wait_for_link_sync_confirmation(nonce)
        if not confirmed:
            logger.warning(
                "MCU link synchronisation did not confirm within timeout"
            )
            pending_nonce = self.state.link_handshake_nonce
            self.state.link_expected_tag = None
            self.state.link_handshake_nonce = None
            self.state.link_nonce_length = 0
            if pending_nonce == nonce:
                await self._handle_handshake_failure("link_sync_timeout")
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
            if (
                self.state.link_handshake_nonce != nonce
                and not self.state.link_is_synchronized
            ):
                break
            await asyncio.sleep(0.01)
        return (
            self.state.link_is_synchronized
            and self.state.link_handshake_nonce is None
        )

    def _clear_handshake_expectations(self) -> None:
        self.state.link_handshake_nonce = None
        self.state.link_expected_tag = None
        self.state.link_nonce_length = 0

    def _handshake_backoff_remaining(self) -> float:
        deadline = self.state.handshake_backoff_until
        if deadline <= 0:
            return 0.0
        return max(0.0, deadline - time.monotonic())

    async def _respect_handshake_backoff(self) -> None:
        delay = self._handshake_backoff_remaining()
        if delay <= 0:
            return
        logger.warning(
            "Delaying serial handshake for %.2fs due to prior failures",
            delay,
        )
        await self._publish_handshake_event(
            "backoff_wait",
            reason=self.state.last_handshake_error,
            detail="waiting_for_backoff",
            extra={"delay_seconds": round(delay, 3)},
        )
        await asyncio.sleep(delay)

    async def _publish_handshake_event(
        self,
        event: str,
        *,
        reason: Optional[str] = None,
        detail: Optional[str] = None,
        extra: Optional[dict[str, Any]] = None,
    ) -> None:
        payload: dict[str, Any] = {
            "event": event,
            "reason": reason,
            "detail": detail,
            "attempts": self.state.handshake_attempts,
            "successes": self.state.handshake_successes,
            "failures": self.state.handshake_failures,
            "failure_streak": self.state.handshake_failure_streak,
            "backoff_until": self.state.handshake_backoff_until,
        }
        if extra:
            payload.update(extra)
        message = (
            PublishableMessage(
                topic_name=handshake_topic(self.state.mqtt_topic_prefix),
                payload=json.dumps(payload).encode("utf-8"),
            )
            .with_content_type("application/json")
            .with_user_property("bridge-event", "handshake")
        )
        await self.enqueue_mqtt(message)

    async def _handle_handshake_success(self) -> None:
        self.state.record_handshake_success()
        await self._publish_handshake_event("success")

    async def _handle_handshake_failure(
        self,
        reason: str,
        *,
        detail: Optional[str] = None,
    ) -> None:
        self.state.record_handshake_failure(reason)
        backoff = self._maybe_schedule_handshake_backoff(reason)
        extra = {"backoff_seconds": round(backoff, 3)} if backoff else None
        await self._publish_handshake_event(
            "failure",
            reason=reason,
            detail=detail,
            extra=extra,
        )

    def _maybe_schedule_handshake_backoff(
        self, reason: str
    ) -> Optional[float]:
        streak = max(1, self.state.handshake_failure_streak)
        fatal = reason in _FATAL_HANDSHAKE_REASONS
        threshold = 1 if fatal else 3
        if streak < threshold:
            return None
        power = max(0, streak - threshold)
        delay = min(
            SERIAL_HANDSHAKE_BACKOFF_MAX,
            SERIAL_HANDSHAKE_BACKOFF_BASE * (2 ** power),
        )
        self.state.handshake_backoff_until = time.monotonic() + delay
        return delay

    def _fatal_handshake_reason(self) -> Optional[str]:
        reason = self.state.last_handshake_error
        if reason in _FATAL_HANDSHAKE_REASONS:
            return reason
        return None

    def _raise_if_handshake_fatal(self) -> None:
        reason = self._fatal_handshake_reason()
        if not reason:
            return

        hint = (
            "Verify YUNBRIDGE_SERIAL_SECRET (usually stored in "
            "/etc/yunbridge/credentials or exported before starting the "
            "daemon) matches the BridgeSecret.h value flashed on the MCU."
        )
        raise SerialHandshakeFatal(
            (
                "MCU rejected the serial shared secret "
                f"(reason={reason}). {hint}"
            )
        )

    def _compute_handshake_tag(self, nonce: bytes) -> bytes:
        secret = self.config.serial_shared_secret
        if not secret:
            return b""
        digest = hmac.new(secret, nonce, hashlib.sha256).digest()
        return digest[:SERIAL_HANDSHAKE_TAG_LEN]

    def _should_acknowledge_mcu_frame(self, command_id: int) -> bool:
        return command_id not in STATUS_VALUES

    def _is_frame_allowed_pre_sync(self, command_id: int) -> bool:
        if self.state.link_is_synchronized:
            return True
        if command_id in STATUS_VALUES:
            return True
        return command_id in _PRE_SYNC_ALLOWED_COMMANDS

    async def _acknowledge_mcu_frame(
        self,
        command_id: int,
        *,
        status: Status = Status.ACK,
        extra: bytes = b"",
    ) -> None:
        payload = pack_u16(command_id)
        if extra:
            remaining = MAX_PAYLOAD_SIZE - len(payload)
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
        except Exception:
            logger.exception(
                "Failed to emit status 0x%02X for command 0x%02X",
                status.value,
                command_id,
            )

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

    async def _dispatch_mqtt_message(self, inbound: InboundMessage) -> None:
        await self._handle_mqtt_topic(inbound)

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
        self.state.record_mcu_status(status)
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
        status_topic = topic_path(
            self.state.mqtt_topic_prefix,
            Topic.SYSTEM,
            Topic.STATUS,
        )
        message = (
            PublishableMessage(
                topic_name=status_topic,
                payload=report,
            )
            .with_content_type("application/json")
            .with_message_expiry(30)
            .with_user_property("bridge-status", status.name)
        )
        if text:
            message = message.with_user_property(
                "bridge-status-message",
                text,
            )
        await self.enqueue_mqtt(message)

    async def _handle_get_free_memory_resp(self, payload: bytes) -> None:
        if len(payload) != 2:
            logger.warning(
                "Malformed GET_FREE_MEMORY_RESP payload: %s", payload.hex()
            )
            return

        free_memory = int.from_bytes(payload, "big")
        topic = topic_path(
            self.state.mqtt_topic_prefix,
            Topic.SYSTEM,
            "free_memory",
            "value",
        )
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
            await self._handle_handshake_failure("unexpected_sync_resp")
            return False

        nonce_length = self.state.link_nonce_length or len(expected)
        required_length = nonce_length + SERIAL_HANDSHAKE_TAG_LEN
        rate_limit = self.config.serial_handshake_min_interval
        if rate_limit > 0:
            now = time.monotonic()
            if now < self.state.handshake_rate_limit_until:
                logger.warning(
                    (
                        "LINK_SYNC_RESP throttled due to rate limit "
                        "(remaining=%.2fs)"
                    ),
                    self.state.handshake_rate_limit_until - now,
                )
                await self._acknowledge_mcu_frame(
                    Command.CMD_LINK_SYNC_RESP.value,
                    status=Status.MALFORMED,
                    extra=payload[: MAX_PAYLOAD_SIZE - 2],
                )
                await self._handle_handshake_failure("sync_rate_limited")
                return False
            self.state.handshake_rate_limit_until = now + rate_limit

        if len(payload) != required_length:
            logger.warning(
                "LINK_SYNC_RESP malformed length (expected %d got %d)",
                required_length,
                len(payload),
            )
            await self._acknowledge_mcu_frame(
                Command.CMD_LINK_SYNC_RESP.value,
                status=Status.MALFORMED,
                extra=payload[: MAX_PAYLOAD_SIZE - 2],
            )
            self._clear_handshake_expectations()
            await self._handle_handshake_failure("sync_length_mismatch")
            return False

        nonce = payload[:nonce_length]
        tag_bytes = payload[nonce_length:required_length]
        expected_tag = self.state.link_expected_tag
        recalculated_tag = self._compute_handshake_tag(nonce)

        if (
            nonce != expected
            or expected_tag is None
            or len(tag_bytes) != SERIAL_HANDSHAKE_TAG_LEN
            or not hmac.compare_digest(tag_bytes, recalculated_tag)
        ):
            logger.warning(
                "LINK_SYNC_RESP auth mismatch (nonce=%s)",
                nonce.hex(),
            )
            await self._acknowledge_mcu_frame(
                Command.CMD_LINK_SYNC_RESP.value,
                status=Status.MALFORMED,
                extra=payload[: MAX_PAYLOAD_SIZE - 2],
            )
            self._clear_handshake_expectations()
            await self._handle_handshake_failure(
                "sync_auth_mismatch",
                detail="nonce_or_tag_mismatch",
            )
            return False

        payload = nonce  # Normalise for logging below

        self.state.link_is_synchronized = True
        self._clear_handshake_expectations()
        await self._handle_handshake_success()
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
        topic = topic_path(
            self.state.mqtt_topic_prefix,
            Topic.SYSTEM,
            "version",
            "value",
        )
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

    async def _handle_mqtt_topic(self, inbound: InboundMessage) -> None:
        topic_name = inbound.topic_name
        route = parse_topic(self.state.mqtt_topic_prefix, topic_name)
        if route is None:
            logger.debug(
                "Ignoring MQTT message with unexpected prefix: %s",
                topic_name,
            )
            return

        if not route.segments:
            logger.debug("MQTT topic missing identifier: %s", topic_name)
            return

        payload = inbound.payload
        payload_str = payload.decode("utf-8", errors="ignore")
        identifier = route.identifier
        remainder = list(route.remainder)
        parts = topic_name.split("/")

        try:
            if route.topic == Topic.FILE and len(route.segments) >= 2:
                if not self._is_topic_action_allowed(route.topic, identifier):
                    await self._reject_topic_action(
                        inbound,
                        route.topic,
                        identifier,
                    )
                    return
                await self._file.handle_mqtt(
                    identifier,
                    remainder,
                    payload,
                    inbound,
                )
            elif route.topic == Topic.CONSOLE and identifier == "in":
                await self._console.handle_mqtt_input(payload, inbound)
            elif route.topic == Topic.DATASTORE:
                if not self._is_topic_action_allowed(route.topic, identifier):
                    await self._reject_topic_action(
                        inbound,
                        route.topic,
                        identifier,
                    )
                    return
                await self._datastore.handle_mqtt(
                    identifier,
                    remainder,
                    payload,
                    payload_str,
                    inbound,
                )
            elif route.topic == Topic.MAILBOX and identifier == "write":
                if not self._is_topic_action_allowed(route.topic, identifier):
                    await self._reject_topic_action(
                        inbound,
                        route.topic,
                        identifier,
                    )
                    return
                await self._mailbox.handle_mqtt_write(payload, inbound)
            elif route.topic == Topic.MAILBOX and identifier == "read":
                if not self._is_topic_action_allowed(route.topic, identifier):
                    await self._reject_topic_action(
                        inbound,
                        route.topic,
                        identifier,
                    )
                    return
                await self._mailbox.handle_mqtt_read(inbound)
            elif route.topic == Topic.SHELL:
                await self._shell.handle_mqtt(parts, payload_str, inbound)
            elif route.topic in (Topic.DIGITAL, Topic.ANALOG):
                await self._pin.handle_mqtt(
                    route.topic,
                    parts,
                    payload_str,
                    inbound,
                )
            elif route.topic == Topic.SYSTEM:
                handled = await self._system.handle_mqtt(
                    identifier,
                    remainder,
                    inbound,
                )
                if not handled:
                    logger.debug("Unhandled MQTT system topic %s", topic_name)
            else:
                logger.debug("Unhandled MQTT topic %s", topic_name)
        except Exception:
            logger.exception("Error processing MQTT topic: %s", topic_name)

    def _is_topic_action_allowed(
        self,
        topic_type: Topic | str,
        action: str,
    ) -> bool:
        if not action:
            return True
        topic_value = (
            topic_type.value if isinstance(topic_type, Topic) else topic_type
        )
        return self.state.topic_authorization.allows(topic_value, action)

    async def _reject_topic_action(
        self,
        inbound: InboundMessage,
        topic_type: Topic | str,
        action: str,
    ) -> None:
        topic_value = (
            topic_type.value if isinstance(topic_type, Topic) else topic_type
        )
        logger.warning(
            "Blocked MQTT action topic=%s action=%s (message topic=%s)",
            topic_value,
            action or "<missing>",
            inbound.topic_name,
        )
        payload = json.dumps(
            {
                "status": "forbidden",
                "topic": topic_value,
                "action": action,
            }
        ).encode("utf-8")
        status_topic = topic_path(
            self.state.mqtt_topic_prefix,
            Topic.SYSTEM,
            Topic.STATUS,
        )
        message = (
            PublishableMessage(
                topic_name=status_topic,
                payload=payload,
            )
            .with_content_type("application/json")
            .with_message_expiry(30)
            .with_user_property("bridge-error", _TOPIC_FORBIDDEN_REASON)
        )
        await self.enqueue_mqtt(message, reply_context=inbound)
