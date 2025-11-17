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
import base64
import json
import logging
import os
import struct
from typing import Awaitable, Callable, Dict, Optional, Tuple
from ..mqtt import PublishableMessage
from yunrpc.protocol import (
    DATASTORE_KEY_LEN_FORMAT,
    DATASTORE_VALUE_LEN_FORMAT,
    DATASTORE_VALUE_LEN_SIZE,
    MAX_PAYLOAD_SIZE,
    Command,
    Status,
)

from ..config.settings import RuntimeConfig
from ..state.context import RuntimeState

SendFrameCallable = Callable[[int, bytes], Awaitable[bool]]

logger = logging.getLogger("yunbridge.service")

STATUS_VALUES = {status.value for status in Status}

TOPIC_DIGITAL = "d"
TOPIC_ANALOG = "a"
TOPIC_CONSOLE = "console"
TOPIC_SH = "sh"
TOPIC_MAILBOX = "mailbox"
TOPIC_MAILBOX_INCOMING_AVAILABLE = "mailbox/incoming_available"
TOPIC_MAILBOX_OUTGOING_AVAILABLE = "mailbox/outgoing_available"
TOPIC_DATASTORE = "datastore"
TOPIC_FILE = "file"
TOPIC_STATUS = "status"


class BridgeService:
    """Service façade used by the transport layer."""

    def __init__(self, config: RuntimeConfig, state: RuntimeState) -> None:
        self.config = config
        self.state = state
        self._serial_sender: Optional[SendFrameCallable] = None
        self._mcu_handlers: Dict[
            int, Callable[[bytes], Awaitable[Optional[bool]]]
        ] = {
            Command.CMD_DIGITAL_READ_RESP.value:
                self._handle_digital_read_resp,
            Command.CMD_ANALOG_READ_RESP.value:
                self._handle_analog_read_resp,
            Command.CMD_GET_VERSION_RESP.value:
                self._handle_get_version_resp,
            Command.CMD_GET_FREE_MEMORY_RESP.value:
                self._handle_get_free_memory_resp,
            Command.CMD_LINK_SYNC_RESP.value: self._handle_link_sync_resp,
            Command.CMD_LINK_RESET_RESP.value: self._handle_link_reset_resp,
            Status.ACK.value: self._handle_ack,
            Status.ERROR.value: self._status_handler(Status.ERROR),
            Status.CMD_UNKNOWN.value: self._status_handler(Status.CMD_UNKNOWN),
            Status.MALFORMED.value: self._status_handler(Status.MALFORMED),
            Status.CRC_MISMATCH.value: self._status_handler(
                Status.CRC_MISMATCH
            ),
            Status.TIMEOUT.value: self._status_handler(Status.TIMEOUT),
            Status.NOT_IMPLEMENTED.value: self._status_handler(
                Status.NOT_IMPLEMENTED
            ),
            Command.CMD_XOFF.value: self._handle_xoff,
            Command.CMD_XON.value: self._handle_xon,
            Command.CMD_CONSOLE_WRITE.value: self._handle_console_write,
            Command.CMD_DATASTORE_GET_RESP.value:
                self._handle_datastore_get_resp,
            Command.CMD_DATASTORE_PUT.value: self._handle_datastore_put,
            Command.CMD_MAILBOX_PROCESSED.value:
                self._handle_mailbox_processed,
            Command.CMD_MAILBOX_PUSH.value: self._handle_mailbox_push,
            Command.CMD_MAILBOX_AVAILABLE.value:
                self._handle_mailbox_available,
            Command.CMD_MAILBOX_READ.value: self._handle_mailbox_read,
            Command.CMD_FILE_WRITE.value: self._handle_file_write,
            Command.CMD_FILE_READ.value: self._handle_file_read,
            Command.CMD_FILE_REMOVE.value: self._handle_file_remove,
            Command.CMD_PROCESS_RUN.value: self._handle_process_run,
            Command.CMD_PROCESS_RUN_ASYNC.value:
                self._handle_process_run_async,
            Command.CMD_PROCESS_POLL.value: self._handle_process_poll,
            Command.CMD_PROCESS_KILL.value: self._handle_process_kill,
        }

    def register_serial_sender(self, sender: SendFrameCallable) -> None:
        """Allow the serial transport to provide its send coroutine."""

        self._serial_sender = sender

    async def send_frame(self, command_id: int, payload: bytes = b"") -> bool:
        if not self._serial_sender:
            logger.error(
                "Serial sender not registered; cannot send frame 0x%02X",
                command_id,
            )
            return False
        return await self._serial_sender(command_id, payload)

    async def on_serial_connected(self) -> None:
        """Run post-connection initialisation for the MCU link."""

        try:
            await self.sync_link()
        except Exception:
            logger.exception("Failed to synchronise MCU link after reconnect")

        try:
            await self.request_mcu_version()
        except Exception:
            logger.exception("Failed to request MCU version after reconnect")

        try:
            await self._flush_console_queue()
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
        self.state.mcu_is_paused = False

    async def handle_mcu_frame(self, command_id: int, payload: bytes) -> None:
        """Entry point invoked by the serial transport for each MCU frame."""

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

    async def request_mcu_version(self) -> None:
        send_ok = await self.send_frame(Command.CMD_GET_VERSION.value, b"")
        if send_ok:
            self.state.mcu_version = None

    async def sync_link(self) -> None:
        nonce = os.urandom(4)
        self.state.link_handshake_nonce = nonce
        self.state.link_is_synchronized = False
        await self.send_frame(Command.CMD_LINK_RESET.value, b"")
        await asyncio.sleep(0.05)
        await self.send_frame(Command.CMD_LINK_SYNC.value, nonce)

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

    async def _handle_digital_read_resp(self, payload: bytes) -> None:
        if len(payload) != 1:
            logger.warning(
                "Malformed DIGITAL_READ_RESP payload: expected 1 byte, got %d",
                len(payload),
            )
            return

        value = payload[0]
        pin: Optional[int] = None
        if self.state.pending_digital_reads:
            pin = self.state.pending_digital_reads.popleft()
        else:
            logger.warning(
                "Received DIGITAL_READ_RESP without pending request."
            )

        topic = (
            f"{self.state.mqtt_topic_prefix}/{TOPIC_DIGITAL}/{pin}/value"
            if pin is not None
            else f"{self.state.mqtt_topic_prefix}/{TOPIC_DIGITAL}/value"
        )
        message = PublishableMessage(
            topic_name=topic,
            payload=str(value).encode("utf-8"),
        )
        await self.enqueue_mqtt(message)

    async def _handle_analog_read_resp(self, payload: bytes) -> None:
        if len(payload) != 2:
            logger.warning(
                "Malformed ANALOG_READ_RESP payload: expected 2 bytes, got %d",
                len(payload),
            )
            return

        value = int.from_bytes(payload, "big")
        pin: Optional[int] = None
        if self.state.pending_analog_reads:
            pin = self.state.pending_analog_reads.popleft()
        else:
            logger.warning(
                "Received ANALOG_READ_RESP without pending request."
            )

        topic = (
            f"{self.state.mqtt_topic_prefix}/{TOPIC_ANALOG}/{pin}/value"
            if pin is not None
            else f"{self.state.mqtt_topic_prefix}/{TOPIC_ANALOG}/value"
        )
        message = PublishableMessage(
            topic_name=topic,
            payload=str(value).encode("utf-8"),
        )
        await self.enqueue_mqtt(message)

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
                    f"{self.state.mqtt_topic_prefix}/system/{TOPIC_STATUS}"
                ),
                payload=report,
            )
        )

    async def _handle_xoff(self, _: bytes) -> None:
        logger.warning("MCU > XOFF received, pausing console output.")
        self.state.mcu_is_paused = True

    async def _handle_xon(self, _: bytes) -> None:
        logger.info("MCU > XON received, resuming console output.")
        self.state.mcu_is_paused = False
        await self._flush_console_queue()

    async def _handle_console_write(self, payload: bytes) -> None:
        topic = f"{self.state.mqtt_topic_prefix}/{TOPIC_CONSOLE}/out"
        message = PublishableMessage(
            topic_name=topic,
            payload=payload,
        )
        await self.enqueue_mqtt(message)

    async def _handle_datastore_get_resp(self, payload: bytes) -> None:
        if len(payload) < DATASTORE_VALUE_LEN_SIZE:
            logger.warning(
                "Malformed DATASTORE_GET_RESP payload: too short (%d bytes)",
                len(payload),
            )
            return

        value_len = payload[0]
        expected_length = DATASTORE_VALUE_LEN_SIZE + value_len
        if len(payload) < expected_length:
            logger.warning(
                "Malformed DATASTORE_GET_RESP payload: expected %d bytes, "
                "got %d",
                expected_length,
                len(payload),
            )
            return

        value_bytes = payload[DATASTORE_VALUE_LEN_SIZE:expected_length]
        key: Optional[str] = None
        if self.state.pending_datastore_gets:
            key = self.state.pending_datastore_gets.popleft()
        else:
            logger.warning("DATASTORE_GET_RESP without pending key tracking.")

        if key:
            value_text = value_bytes.decode("utf-8", errors="ignore")
            self.state.datastore[key] = value_text
            topic = (
                f"{self.state.mqtt_topic_prefix}/{TOPIC_DATASTORE}/get/{key}"
            )
            message = PublishableMessage(
                topic_name=topic,
                payload=value_bytes,
            )
            await self.enqueue_mqtt(message)
        else:
            logger.debug(
                "DATASTORE_GET_RESP value=%s",
                value_bytes.decode("utf-8", errors="ignore"),
            )

    async def _handle_datastore_put(self, payload: bytes) -> None:
        if len(payload) < 2:
            logger.warning(
                "Malformed DATASTORE_PUT payload: too short (%d bytes)",
                len(payload),
            )
            return

        key_len = payload[0]
        cursor = 1
        if len(payload) < cursor + key_len + DATASTORE_VALUE_LEN_SIZE:
            logger.warning(
                "Malformed DATASTORE_PUT payload: missing key/value data."
            )
            return

        key_bytes = payload[cursor:cursor + key_len]
        cursor += key_len
        value_len = payload[cursor]
        cursor += DATASTORE_VALUE_LEN_SIZE

        remaining = len(payload) - cursor
        if remaining < value_len:
            logger.warning(
                "Malformed DATASTORE_PUT payload: value length mismatch."
            )
            return

        value_bytes = payload[cursor:cursor + value_len]
        key = key_bytes.decode("utf-8", errors="ignore")
        value = value_bytes.decode("utf-8", errors="ignore")
        self.state.datastore[key] = value
        topic = f"{self.state.mqtt_topic_prefix}/{TOPIC_DATASTORE}/get/{key}"
        message = PublishableMessage(
            topic_name=topic,
            payload=value_bytes,
        )
        await self.enqueue_mqtt(message)

    async def _handle_mailbox_processed(self, payload: bytes) -> bool:
        topic_name = (
            f"{self.state.mqtt_topic_prefix}/{TOPIC_MAILBOX}/processed"
        )
        message_id: Optional[int] = None
        if len(payload) >= 2:
            (message_id,) = struct.unpack(">H", payload[:2])

        if message_id is not None:
            body = json.dumps({"message_id": message_id}).encode("utf-8")
        else:
            body = payload

        await self.enqueue_mqtt(
            PublishableMessage(topic_name=topic_name, payload=body)
        )
        return True

    async def _handle_mailbox_push(self, payload: bytes) -> bool:
        if len(payload) < 2:
            logger.warning("Malformed MAILBOX_PUSH payload: %s", payload.hex())
            return False

        (msg_len,) = struct.unpack(">H", payload[:2])
        data = payload[2:2 + msg_len]
        if len(data) != msg_len:
            logger.warning(
                "MAILBOX_PUSH length mismatch. Expected %d bytes, got %d.",
                msg_len,
                len(data),
            )
            return False

        stored = self.state.enqueue_mailbox_incoming(data, logger)
        if not stored:
            logger.error(
                "Dropping incoming mailbox message (%d bytes) due to "
                "queue limits.",
                len(data),
            )
            await self.send_frame(
                Status.ERROR.value,
                self._encode_status_reason("mailbox_incoming_overflow"),
            )
            return False

        topic = self.state.mailbox_incoming_topic or (
            f"{self.state.mqtt_topic_prefix}/{TOPIC_MAILBOX}/incoming"
        )
        await self.enqueue_mqtt(
            PublishableMessage(topic_name=topic, payload=data)
        )

        await self.enqueue_mqtt(
            PublishableMessage(
                topic_name=(
                    f"{self.state.mqtt_topic_prefix}/"
                    f"{TOPIC_MAILBOX_INCOMING_AVAILABLE}"
                ),
                payload=str(
                    len(self.state.mailbox_incoming_queue)
                ).encode("utf-8"),
            )
        )
        return True

    async def _handle_mailbox_available(self, _: bytes) -> None:
        queue_len = len(self.state.mailbox_queue) & 0xFF
        count_payload = struct.pack(">B", queue_len)
        await self.send_frame(
            Command.CMD_MAILBOX_AVAILABLE_RESP.value,
            count_payload,
        )

    async def _handle_mailbox_read(self, _: bytes) -> bool:
        original_payload = self.state.pop_mailbox_message()
        message_payload = (
            original_payload if original_payload is not None else b""
        )

        msg_len = len(message_payload)
        if msg_len > MAX_PAYLOAD_SIZE - 2:
            logger.warning(
                "Mailbox message too long (%d bytes), truncating.", msg_len
            )
            message_payload = message_payload[: MAX_PAYLOAD_SIZE - 2]
            msg_len = len(message_payload)

        response_payload = struct.pack(">H", msg_len) + message_payload
        send_ok = await self.send_frame(
            Command.CMD_MAILBOX_READ_RESP.value,
            response_payload,
        )

        if not send_ok:
            if original_payload is not None:
                self.state.requeue_mailbox_message_front(original_payload)
            return False

        await self.enqueue_mqtt(
            PublishableMessage(
                topic_name=(
                    f"{self.state.mqtt_topic_prefix}/"
                    f"{TOPIC_MAILBOX_OUTGOING_AVAILABLE}"
                ),
                payload=str(len(self.state.mailbox_queue)).encode("utf-8"),
            )
        )
        return True

    async def _handle_file_write(self, payload: bytes) -> bool:
        if len(payload) < 3:
            logger.warning(
                "Invalid file write payload length: %d", len(payload)
            )
            return False

        path_len = payload[0]
        cursor = 1
        if len(payload) < cursor + path_len + 2:
            logger.warning("Invalid file write payload: missing data section")
            return False

        path = payload[cursor:cursor + path_len].decode(
            "utf-8", errors="ignore"
        )
        cursor += path_len
        data_len = int.from_bytes(payload[cursor:cursor + 2], "big")
        cursor += 2

        file_data = payload[cursor:cursor + data_len]
        if len(file_data) != data_len:
            logger.warning(
                "File write payload truncated. Expected %d bytes.", data_len
            )
            return False

        success, _, reason = await self._perform_file_operation(
            "write", path, file_data
        )
        if success:
            return True

        await self.send_frame(
            Status.ERROR.value,
            self._encode_status_reason(reason or "write_failed"),
        )
        return False

    async def _handle_file_read(self, payload: bytes) -> None:
        if len(payload) < 1:
            logger.warning(
                "Invalid file read payload length: %d", len(payload)
            )
            return

        path_len = payload[0]
        if len(payload) < 1 + path_len:
            logger.warning("Invalid file read payload: missing path bytes")
            return

        filename = payload[1:1 + path_len].decode("utf-8", errors="ignore")
        success, content, reason = await self._perform_file_operation(
            "read", filename
        )

        if not success:
            await self.send_frame(
                Status.ERROR.value,
                self._encode_status_reason(reason or "read_failed"),
            )
            return

        data = content or b""
        max_payload = MAX_PAYLOAD_SIZE - 2
        if len(data) > max_payload:
            logger.warning(
                "File read response truncated from %d to %d bytes for %s",
                len(data),
                max_payload,
                filename,
            )
            data = data[:max_payload]
        response = struct.pack(">H", len(data)) + data
        await self.send_frame(Command.CMD_FILE_READ_RESP.value, response)

    async def _handle_file_remove(self, payload: bytes) -> bool:
        if len(payload) < 1:
            logger.warning(
                "Invalid file remove payload length: %d", len(payload)
            )
            return False

        path_len = payload[0]
        if len(payload) < 1 + path_len:
            logger.warning("Invalid file remove payload: missing path bytes")
            return False

        filename = payload[1:1 + path_len].decode("utf-8", errors="ignore")
        success, _, reason = await self._perform_file_operation(
            "remove", filename
        )
        if success:
            return True

        await self.send_frame(
            Status.ERROR.value,
            self._encode_status_reason(reason or "remove_failed"),
        )
        return False

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
            return False

        self.state.link_is_synchronized = True
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

    async def _handle_process_run(self, payload: bytes) -> None:
        command = payload.decode("utf-8", errors="ignore")
        (
            status,
            stdout_bytes,
            stderr_bytes,
            exit_code,
        ) = await self._run_command_sync(command)

        max_payload = MAX_PAYLOAD_SIZE - 5
        stdout_trim = stdout_bytes[:max_payload]
        remaining = max_payload - len(stdout_trim)
        stderr_trim = stderr_bytes[:remaining]

        response = (
            bytes([status & 0xFF])
            + struct.pack(">H", len(stdout_trim))
            + stdout_trim
            + struct.pack(">H", len(stderr_trim))
            + stderr_trim
        )

        await self.send_frame(Command.CMD_PROCESS_RUN_RESP.value, response)
        logger.debug(
            "Sent PROCESS_RUN_RESP status=%d exit=%s", status, exit_code
        )

    async def _handle_process_run_async(self, payload: bytes) -> None:
        command = payload.decode("utf-8", errors="ignore")
        pid = await self._start_async_process(command)
        await self.send_frame(
            Command.CMD_PROCESS_RUN_ASYNC_RESP.value, struct.pack(">H", pid)
        )
        response_topic = (
            f"{self.state.mqtt_topic_prefix}/{TOPIC_SH}/run_async/response"
        )
        response_payload = str(pid).encode("utf-8")
        await self.enqueue_mqtt(
            PublishableMessage(
                topic_name=response_topic,
                payload=response_payload,
            )
        )

    async def _handle_process_poll(self, payload: bytes) -> bool:
        if len(payload) != 2:
            logger.warning(
                "Invalid PROCESS_POLL payload. Expected 2 bytes, got %d: %s",
                len(payload),
                payload.hex(),
            )
            response_payload = struct.pack(
                ">BBHH", Status.MALFORMED.value, 0xFF, 0, 0
            )
            await self.send_frame(
                Command.CMD_PROCESS_POLL_RESP.value, response_payload
            )
            return False

        pid = int.from_bytes(payload, "big")
        (
            status_byte,
            exit_code,
            stdout_buffer,
            stderr_buffer,
            finished,
            stdout_truncated,
            stderr_truncated,
        ) = await self._collect_process_output(pid)

        response_payload = (
            struct.pack(
                ">BBHH",
                status_byte,
                exit_code,
                len(stdout_buffer),
                len(stderr_buffer),
            )
            + stdout_buffer
            + stderr_buffer
        )

        await self.send_frame(
            Command.CMD_PROCESS_POLL_RESP.value, response_payload
        )

        await self._publish_process_poll_result(
            pid,
            status_byte,
            exit_code,
            stdout_buffer,
            stderr_buffer,
            stdout_truncated,
            stderr_truncated,
            finished,
        )

        if finished:
            logger.debug("Sent final output for finished process PID %d", pid)
        return True

    async def _handle_process_kill(
        self,
        payload: bytes,
        *,
        send_ack: bool = True,
    ) -> bool:
        if len(payload) != 2:
            logger.warning(
                "Invalid PROCESS_KILL payload. Expected 2 bytes, got %d: %s",
                len(payload),
                payload.hex(),
            )
            return False

        pid = int.from_bytes(payload, "big")

        async with self.state.process_lock:
            if pid in self.state.running_processes:
                proc_to_kill = self.state.running_processes[pid]
                try:
                    proc_to_kill.kill()
                    async with asyncio.timeout(0.5):
                        await proc_to_kill.wait()
                    logger.info("Killed process with PID %d", pid)
                except asyncio.TimeoutError:
                    logger.warning(
                        "Process PID %d did not terminate after kill signal.",
                        pid,
                    )
                except ProcessLookupError:
                    logger.info(
                        "Process PID %d already exited before kill.", pid
                    )
                except Exception:
                    logger.exception("Error killing process PID %d", pid)
                finally:
                    if pid in self.state.running_processes:
                        del self.state.running_processes[pid]
            else:
                logger.warning("Attempted to kill non-existent PID: %d", pid)

        return send_ack

    async def _allocate_pid(self) -> int:
        async with self.state.process_lock:
            for _ in range(0xFFFF):
                candidate = self.state.next_pid & 0xFFFF
                self.state.next_pid = (candidate + 1) & 0xFFFF
                if self.state.next_pid == 0:
                    self.state.next_pid = 1
                if candidate == 0:
                    continue
                if candidate not in self.state.running_processes:
                    return candidate

        logger.error("No async process slots available; all PIDs in use")
        return 0xFFFF

    def _is_command_allowed(self, command: str) -> bool:
        parts = command.strip().split()
        if not parts:
            return False
        if not self.state.allowed_commands:
            return True
        return parts[0] in self.state.allowed_commands

    async def _run_command_sync(
        self, command: str
    ) -> Tuple[int, bytes, bytes, Optional[int]]:
        if not command.strip():
            return Status.MALFORMED.value, b"", b"Empty command", None

        if not self._is_command_allowed(command):
            error_msg = (
                f"Command '{command.split()[0]}' not allowed".encode("utf-8")
            )
            return Status.ERROR.value, b"", error_msg, None

        try:
            proc = await asyncio.create_subprocess_shell(
                command,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
        except OSError as exc:
            return (
                Status.ERROR.value,
                b"",
                str(exc).encode("utf-8", errors="ignore"),
                None,
            )

        try:
            async with asyncio.TaskGroup() as tg:
                comm_task = tg.create_task(proc.communicate())
                try:
                    async with asyncio.timeout(self.state.process_timeout):
                        await comm_task
                except asyncio.TimeoutError:
                    proc.kill()
                    # Allow communicate() to finish after kill (brief grace).
                    async with asyncio.timeout(1):
                        await comm_task
                    # If it's still not done, it's a hard timeout
                    if not comm_task.done():
                        return (
                            Status.TIMEOUT.value,
                            b"",
                            b"Timeout after kill",
                            None,
                        )
                    # If it finished, retrieve results but still report timeout
                    stdout_bytes, stderr_bytes = comm_task.result()
                    return (
                        Status.TIMEOUT.value,
                        stdout_bytes,
                        stderr_bytes,
                        proc.returncode,
                    )

            stdout_bytes, stderr_bytes = comm_task.result()
            status = Status.OK.value

        except Exception:
            logger.exception(
                "Unexpected error executing command '%s'", command
            )
            return Status.ERROR.value, b"", b"Internal error", None

        return (
            status,
            stdout_bytes or b"",
            stderr_bytes or b"",
            proc.returncode,
        )

    async def _start_async_process(self, command: str) -> int:
        if not command.strip():
            return 0xFFFF
        if not self._is_command_allowed(command):
            return 0xFFFF

        pid = await self._allocate_pid()
        if pid == 0xFFFF:
            return 0xFFFF

        try:
            proc = await asyncio.create_subprocess_shell(
                command,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
        except OSError as exc:
            logger.warning(
                "Failed to start async process '%s': %s", command, exc
            )
            return 0xFFFF
        except Exception:
            logger.exception(
                "Unexpected error starting async process '%s'", command
            )
            return 0xFFFF

        async with self.state.process_lock:
            self.state.running_processes[pid] = proc
            self.state.process_stdout_buffer[pid] = bytearray()
            self.state.process_stderr_buffer[pid] = bytearray()
            self.state.process_exit_codes.pop(pid, None)

        logger.info("Started async process '%s' with PID %d", command, pid)
        return pid

    async def _collect_process_output(
        self, pid: int
    ) -> Tuple[int, int, bytes, bytes, bool, bool, bool]:
        async def _read_chunk(
            reader: Optional[asyncio.StreamReader],
            *,
            chunk_size: int = 1024,
        ) -> bytes:
            if reader is None:
                return b""
            try:
                async with asyncio.timeout(0.05):
                    return await reader.read(chunk_size)
            except asyncio.TimeoutError:
                return b""
            except (OSError, ValueError, BrokenPipeError):
                logger.debug(
                    "Error reading process pipe for PID %d",
                    pid,
                    exc_info=True,
                )
                return b""

        async def _drain_reader(
            reader: Optional[asyncio.StreamReader],
        ) -> bytes:
            if reader is None:
                return b""
            drained = bytearray()
            while True:
                chunk = await _read_chunk(reader)
                if not chunk:
                    break
                drained.extend(chunk)
                if len(chunk) < 1024:
                    break
            return bytes(drained)

        async with self.state.process_lock:
            proc = self.state.running_processes.get(pid)
            stdout_buffer = self.state.process_stdout_buffer.setdefault(
                pid, bytearray()
            )
            stderr_buffer = self.state.process_stderr_buffer.setdefault(
                pid, bytearray()
            )
            cached_exit_code = self.state.process_exit_codes.get(pid)

        if not proc:
            if cached_exit_code is None:
                logger.debug("PROCESS_POLL received for unknown PID %d", pid)
                return (
                    Status.ERROR.value,
                    0xFF,
                    b"",
                    b"",
                    False,
                    False,
                    False,
                )

            async with self.state.process_lock:
                stdout_buffer = self.state.process_stdout_buffer.setdefault(
                    pid, bytearray()
                )
                stderr_buffer = self.state.process_stderr_buffer.setdefault(
                    pid, bytearray()
                )
                (
                    stdout_trim,
                    stderr_trim,
                    stdout_truncated,
                    stderr_truncated,
                ) = self._trim_process_buffers(stdout_buffer, stderr_buffer)

                buffers_empty = not stdout_buffer and not stderr_buffer
                exit_code_value = self.state.process_exit_codes.get(pid, 0xFF)
                if buffers_empty:
                    self.state.process_stdout_buffer.pop(pid, None)
                    self.state.process_stderr_buffer.pop(pid, None)
                    self.state.process_exit_codes.pop(pid, None)

            return (
                Status.OK.value,
                exit_code_value & 0xFF,
                stdout_trim,
                stderr_trim,
                True,
                stdout_truncated,
                stderr_truncated,
            )

        stdout_collected = bytearray()
        stderr_collected = bytearray()

        stdout_chunk = await _read_chunk(proc.stdout)
        if stdout_chunk:
            stdout_collected.extend(stdout_chunk)

        stderr_chunk = await _read_chunk(proc.stderr)
        if stderr_chunk:
            stderr_collected.extend(stderr_chunk)

        process_finished = proc.returncode is not None

        if process_finished:
            stdout_remaining = await _drain_reader(proc.stdout)
            if stdout_remaining:
                stdout_collected.extend(stdout_remaining)
            stderr_remaining = await _drain_reader(proc.stderr)
            if stderr_remaining:
                stderr_collected.extend(stderr_remaining)

        exit_code_value = 0xFF
        log_process_finished = False

        async with self.state.process_lock:
            stdout_buffer = self.state.process_stdout_buffer.setdefault(
                pid, bytearray()
            )
            stderr_buffer = self.state.process_stderr_buffer.setdefault(
                pid, bytearray()
            )

            if stdout_collected:
                stdout_buffer.extend(stdout_collected)
            if stderr_collected:
                stderr_buffer.extend(stderr_collected)

            if process_finished:
                exit_code_value = (
                    proc.returncode if proc.returncode is not None else 0
                )
                if pid in self.state.running_processes:
                    self.state.running_processes.pop(pid, None)
                    log_process_finished = True
                self.state.process_exit_codes[pid] = exit_code_value
            else:
                exit_code_value = 0xFF

            (
                stdout_trim,
                stderr_trim,
                stdout_truncated,
                stderr_truncated,
            ) = self._trim_process_buffers(stdout_buffer, stderr_buffer)

            buffers_empty = not stdout_buffer and not stderr_buffer
            if process_finished and buffers_empty:
                self.state.process_stdout_buffer.pop(pid, None)
                self.state.process_stderr_buffer.pop(pid, None)
                self.state.process_exit_codes.pop(pid, None)

        if log_process_finished:
            logger.info(
                "Async process %d finished with exit code %d",
                pid,
                exit_code_value,
            )

        return (
            Status.OK.value,
            exit_code_value & 0xFF,
            stdout_trim,
            stderr_trim,
            process_finished,
            stdout_truncated,
            stderr_truncated,
        )

    def _trim_process_buffers(
        self, stdout_buffer: bytearray, stderr_buffer: bytearray
    ) -> Tuple[bytes, bytes, bool, bool]:
        max_payload = MAX_PAYLOAD_SIZE - 6
        stdout_len = min(len(stdout_buffer), max_payload)
        stdout_trim = bytes(stdout_buffer[:stdout_len])
        del stdout_buffer[:stdout_len]

        remaining = max_payload - len(stdout_trim)
        stderr_len = min(len(stderr_buffer), remaining)
        stderr_trim = bytes(stderr_buffer[:stderr_len])
        del stderr_buffer[:stderr_len]

        stdout_truncated = len(stdout_buffer) > 0
        stderr_truncated = len(stderr_buffer) > 0
        return stdout_trim, stderr_trim, stdout_truncated, stderr_truncated

    async def _publish_process_poll_result(
        self,
        pid: int,
        status_byte: int,
        exit_code: int,
        stdout_trim: bytes,
        stderr_trim: bytes,
        stdout_truncated: bool,
        stderr_truncated: bool,
        finished: bool,
    ) -> None:
        mqtt_topic = (
            f"{self.state.mqtt_topic_prefix}/{TOPIC_SH}/poll/{pid}/response"
        )
        stdout_text = stdout_trim.decode("utf-8", errors="replace")
        stderr_text = stderr_trim.decode("utf-8", errors="replace")
        stdout_b64 = base64.b64encode(stdout_trim).decode("ascii")
        stderr_b64 = base64.b64encode(stderr_trim).decode("ascii")
        mqtt_payload = json.dumps(
            {
                "status": status_byte,
                "exit_code": exit_code,
                "stdout": stdout_text,
                "stderr": stderr_text,
                "stdout_base64": stdout_b64,
                "stderr_base64": stderr_b64,
                "stdout_truncated": stdout_truncated,
                "stderr_truncated": stderr_truncated,
                "finished": finished,
            }
        ).encode("utf-8")
        await self.enqueue_mqtt(
            PublishableMessage(
                topic_name=mqtt_topic,
                payload=mqtt_payload,
            )
        )

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
                await self._handle_mqtt_file(identifier, parts[3:], payload)
            elif topic_type == TOPIC_CONSOLE and identifier == "in":
                await self._handle_mqtt_console_input(payload)
            elif topic_type == TOPIC_DATASTORE and len(parts) >= 3:
                await self._handle_mqtt_datastore(
                    identifier, parts[3:], payload_str
                )
            elif topic_type == TOPIC_MAILBOX and identifier == "write":
                await self._handle_mqtt_mailbox_write(payload)
            elif topic_type == TOPIC_MAILBOX and identifier == "read":
                await self._handle_mqtt_mailbox_read()
            elif topic_type == TOPIC_SH:
                await self._handle_mqtt_shell(parts, payload_str)
            elif topic_type in (TOPIC_DIGITAL, TOPIC_ANALOG):
                await self._handle_mqtt_pin(topic_type, parts, payload_str)
            elif (
                topic_type == "system"
                and identifier == "free_memory"
                and len(parts) >= 4
                and parts[3] == "get"
            ):
                await self.send_frame(Command.CMD_GET_FREE_MEMORY.value, b"")
            elif (
                topic_type == "system"
                and identifier == "version"
                and len(parts) >= 4
                and parts[3] == "get"
            ):
                cached_version = self.state.mcu_version
                await self.request_mcu_version()
                if cached_version is not None:
                    major, minor = cached_version
                    await self.enqueue_mqtt(
                        PublishableMessage(
                            topic_name=(
                                f"{self.state.mqtt_topic_prefix}"
                                "/system/version/value"
                            ),
                            payload=f"{major}.{minor}".encode("utf-8"),
                        )
                    )
            else:
                logger.debug("Unhandled MQTT topic %s", topic)
        except Exception:
            logger.exception("Error processing MQTT topic: %s", topic)

    async def _handle_mqtt_file(
        self, action: str, path_parts: list[str], payload: bytes
    ) -> None:
        filename = "/".join(path_parts)
        if not filename:
            logger.warning("MQTT file action missing filename for %s", action)
            return

        if action == "write":
            success, _, reason = await self._perform_file_operation(
                "write", filename, payload
            )
            if not success:
                logger.error(
                    "MQTT file write failed for %s: %s",
                    filename,
                    reason or "unknown_reason",
                )
        elif action == "read":
            success, content, reason = await self._perform_file_operation(
                "read", filename
            )
            if not success:
                logger.error(
                    "MQTT file read failed for %s: %s",
                    filename,
                    reason or "unknown_reason",
                )
                return
            data = content or b""
            response_topic = (
                f"{self.state.mqtt_topic_prefix}/{TOPIC_FILE}/read/response/"
                f"{filename}"
            )
            await self.enqueue_mqtt(
                PublishableMessage(
                    topic_name=response_topic,
                    payload=data,
                )
            )
        elif action == "remove":
            success, _, reason = await self._perform_file_operation(
                "remove", filename
            )
            if not success:
                logger.error(
                    "MQTT file remove failed for %s: %s",
                    filename,
                    reason or "unknown_reason",
                )
        else:
            logger.debug("Ignoring unknown file action '%s'", action)

    def _iter_console_chunks(self, payload: bytes) -> list[bytes]:
        if not payload:
            return []
        max_size = MAX_PAYLOAD_SIZE
        return [
            payload[index:index + max_size]
            for index in range(0, len(payload), max_size)
        ]

    async def _flush_console_queue(self) -> None:
        while self.state.console_to_mcu_queue and not self.state.mcu_is_paused:
            buffered = self.state.pop_console_chunk()
            chunks = self._iter_console_chunks(buffered)
            for index, chunk in enumerate(chunks):
                if not chunk:
                    continue
                send_ok = await self.send_frame(
                    Command.CMD_CONSOLE_WRITE.value, chunk
                )
                if not send_ok:
                    unsent = b"".join(chunks[index:])
                    if unsent:
                        self.state.requeue_console_chunk_front(unsent)
                    logger.warning(
                        "Serial send failed while flushing console; "
                        "chunk requeued"
                    )
                    # Abort flushing until the serial link recovers to avoid
                    # tight retry loops.
                    return

    async def _handle_mqtt_console_input(self, payload: bytes) -> None:
        chunks = self._iter_console_chunks(payload)
        if self.state.mcu_is_paused:
            logger.warning(
                "MCU paused, queueing %d console chunk(s) (%d bytes)",
                len(chunks),
                len(payload),
            )
            for chunk in chunks:
                if chunk:
                    self.state.enqueue_console_chunk(chunk, logger)
            return

        for index, chunk in enumerate(chunks):
            if not chunk:
                continue
            send_ok = await self.send_frame(
                Command.CMD_CONSOLE_WRITE.value, chunk
            )
            if not send_ok:
                remaining = b"".join(chunks[index:])
                if remaining:
                    self.state.enqueue_console_chunk(remaining, logger)
                logger.warning(
                    "Serial send failed for console input; "
                    "payload queued for retry"
                )
                break

    async def _handle_mqtt_datastore(
        self, action: str, key_parts: list[str], value_text: str
    ) -> None:
        is_request = False
        if action == "get" and key_parts and key_parts[-1] == "request":
            key_parts = key_parts[:-1]
            is_request = True

        key = "/".join(key_parts)
        if not key:
            logger.debug("Ignoring datastore action '%s' without key", action)
            return

        if action == "put":
            key_bytes = key.encode("utf-8")
            value_bytes = value_text.encode("utf-8")

            if len(key_bytes) > 255 or len(value_bytes) > 255:
                logger.warning(
                    "Datastore payload too large. key=%d value=%d",
                    len(key_bytes),
                    len(value_bytes),
                )
                return

            rpc_payload = (
                struct.pack(DATASTORE_KEY_LEN_FORMAT, len(key_bytes))
                + key_bytes
                + struct.pack(DATASTORE_VALUE_LEN_FORMAT, len(value_bytes))
                + value_bytes
            )

            await self.send_frame(Command.CMD_DATASTORE_PUT.value, rpc_payload)
            self.state.datastore[key] = value_text
            topic_name = "/".join(
                [self.state.mqtt_topic_prefix, TOPIC_DATASTORE, "get", key]
            )
            await self.enqueue_mqtt(
                PublishableMessage(
                    topic_name=topic_name,
                    payload=value_bytes,
                )
            )
        elif action == "get":
            key_bytes = key.encode("utf-8")
            if len(key_bytes) > 255:
                logger.warning(
                    "Datastore key too large for GET request (%d bytes)",
                    len(key_bytes),
                )
                return

            payload = (
                struct.pack(DATASTORE_KEY_LEN_FORMAT, len(key_bytes))
                + key_bytes
            )

            send_ok = await self.send_frame(
                Command.CMD_DATASTORE_GET.value,
                payload,
            )
            if send_ok:
                self.state.pending_datastore_gets.append(key)

            cached_value = self.state.datastore.get(key)
            if cached_value is not None:
                topic_name = "/".join(
                    [self.state.mqtt_topic_prefix, TOPIC_DATASTORE, "get", key]
                )
                await self.enqueue_mqtt(
                    PublishableMessage(
                        topic_name=topic_name,
                        payload=cached_value.encode("utf-8"),
                    )
                )
            elif is_request:
                topic_name = "/".join(
                    [self.state.mqtt_topic_prefix, TOPIC_DATASTORE, "get", key]
                )
                await self.enqueue_mqtt(
                    PublishableMessage(
                        topic_name=topic_name,
                        payload=b"",
                    )
                )
        else:
            logger.debug("Unknown datastore action '%s'", action)

    async def _handle_mqtt_mailbox_write(self, payload: bytes) -> None:
        if not self.state.enqueue_mailbox_message(payload, logger):
            logger.error(
                "Failed to enqueue MQTT mailbox payload (%d bytes); "
                "queue full.",
                len(payload),
            )
            return
        logger.info(
            "Added message to mailbox queue. Size=%d",
            len(self.state.mailbox_queue),
        )
        await self.enqueue_mqtt(
            PublishableMessage(
                topic_name=(
                    f"{self.state.mqtt_topic_prefix}/"
                    f"{TOPIC_MAILBOX_OUTGOING_AVAILABLE}"
                ),
                payload=str(len(self.state.mailbox_queue)).encode("utf-8"),
            )
        )

    async def _handle_mqtt_mailbox_read(self) -> None:
        topic = self.state.mailbox_incoming_topic or (
            f"{self.state.mqtt_topic_prefix}/{TOPIC_MAILBOX}/incoming"
        )

        if self.state.mailbox_incoming_queue:
            message_payload = self.state.pop_mailbox_incoming()
            if message_payload is None:
                await self.enqueue_mqtt(
                    PublishableMessage(
                        topic_name=(
                            f"{self.state.mqtt_topic_prefix}/"
                            f"{TOPIC_MAILBOX_INCOMING_AVAILABLE}"
                        ),
                        payload=str(
                            len(self.state.mailbox_incoming_queue)
                        ).encode("utf-8"),
                    )
                )
                return

            await self.enqueue_mqtt(
                PublishableMessage(
                    topic_name=topic,
                    payload=message_payload,
                )
            )

            await self.enqueue_mqtt(
                PublishableMessage(
                    topic_name=(
                        f"{self.state.mqtt_topic_prefix}/"
                        f"{TOPIC_MAILBOX_INCOMING_AVAILABLE}"
                    ),
                    payload=str(
                        len(self.state.mailbox_incoming_queue)
                    ).encode("utf-8"),
                )
            )
            return

        message_payload = self.state.pop_mailbox_message()
        if message_payload is None:
            return

        await self.enqueue_mqtt(
            PublishableMessage(
                topic_name=topic,
                payload=message_payload,
            )
        )

        await self.enqueue_mqtt(
            PublishableMessage(
                topic_name=(
                    f"{self.state.mqtt_topic_prefix}/"
                    f"{TOPIC_MAILBOX_OUTGOING_AVAILABLE}"
                ),
                payload=str(len(self.state.mailbox_queue)).encode("utf-8"),
            )
        )

    async def _handle_mqtt_shell(
        self, parts: list[str], payload_str: str
    ) -> None:
        action = parts[2] if len(parts) >= 3 else ""

        if action == "run":
            if not payload_str:
                return
            await self._handle_shell_run(payload_str)
        elif action == "run_async":
            if not payload_str:
                return
            logger.info("MQTT async shell command: '%s'", payload_str)
            pid = await self._start_async_process(payload_str)
            response_topic = (
                f"{self.state.mqtt_topic_prefix}/{TOPIC_SH}/run_async/response"
            )
            await self.enqueue_mqtt(
                PublishableMessage(
                    topic_name=response_topic,
                    payload=str(pid).encode("utf-8"),
                )
            )
        elif action == "poll" and len(parts) == 4:
            pid_str = parts[3]
            try:
                pid = int(pid_str)
            except ValueError:
                logger.warning("Invalid MQTT PROCESS_POLL PID: %s", pid_str)
                return

            (
                status_byte,
                exit_code,
                stdout_buffer,
                stderr_buffer,
                finished,
                stdout_truncated,
                stderr_truncated,
            ) = await self._collect_process_output(pid)

            await self._publish_process_poll_result(
                pid,
                status_byte,
                exit_code,
                stdout_buffer,
                stderr_buffer,
                stdout_truncated,
                stderr_truncated,
                finished,
            )
        elif action == "kill" and len(parts) == 4:
            pid_str = parts[3]
            try:
                pid_bytes = struct.pack(">H", int(pid_str))
            except ValueError:
                logger.warning("Invalid MQTT PROCESS_KILL PID: %s", pid_str)
                return
            await self._handle_process_kill(pid_bytes, send_ack=False)
        else:
            logger.debug("Ignoring shell topic action: %s", "/".join(parts))

    async def _handle_shell_run(self, command: str) -> None:
        logger.info("Executing shell command from MQTT: '%s'", command)
        response = ""
        proc: Optional[asyncio.subprocess.Process] = None
        try:
            cmd_parts = command.split()
            cmd_base = cmd_parts[0] if cmd_parts else ""
            if (
                self.state.allowed_commands
                and cmd_base not in self.state.allowed_commands
            ):
                raise PermissionError(f"Command '{cmd_base}' not allowed")

            proc = await asyncio.create_subprocess_shell(
                command,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            async with asyncio.timeout(self.state.process_timeout):
                stdout, stderr = await proc.communicate()
            stdout = stdout or b""
            stderr = stderr or b""
            response = (
                f"Exit Code: {proc.returncode}\n-- STDOUT --\n"
                f"{stdout.decode(errors='ignore')}\n-- STDERR --\n"
                f"{stderr.decode(errors='ignore')}"
            )
        except asyncio.TimeoutError:
            response = (
                "Error: Command timed out after "
                f"{self.state.process_timeout} seconds."
            )
            if proc and proc.returncode is None:
                try:
                    proc.kill()
                except ProcessLookupError:
                    pass
        except PermissionError as exc:
            response = f"Error: {exc}"
        except OSError as exc:
            response = f"Error: Failed to execute command: {exc}"
        except Exception:
            logger.exception("Unexpected error executing shell command")
            response = "Error: Unexpected server error"

        await self.enqueue_mqtt(
            PublishableMessage(
                topic_name=(
                    f"{self.state.mqtt_topic_prefix}/{TOPIC_SH}/response"
                ),
                payload=response.encode("utf-8"),
            )
        )

    async def _handle_mqtt_pin(
        self, topic_type: str, parts: list[str], payload_str: str
    ) -> None:
        if len(parts) < 3:
            return

        pin_str = parts[2]
        pin = self._parse_pin_identifier(pin_str)
        if pin < 0:
            return

        if len(parts) == 4:
            subtopic = parts[3]
            if subtopic == "mode" and topic_type == TOPIC_DIGITAL:
                try:
                    mode = int(payload_str)
                except ValueError:
                    logger.warning("Invalid mode payload for pin %s", pin_str)
                    return
                if mode not in [0, 1, 2]:
                    logger.warning("Invalid digital mode %s", mode)
                    return
                await self.send_frame(
                    Command.CMD_SET_PIN_MODE.value,
                    struct.pack(">BB", pin, mode),
                )
            elif subtopic == "read":
                command = (
                    Command.CMD_DIGITAL_READ
                    if topic_type == TOPIC_DIGITAL
                    else Command.CMD_ANALOG_READ
                )
                send_ok = await self.send_frame(
                    command.value, struct.pack(">B", pin)
                )
                if send_ok:
                    if command == Command.CMD_DIGITAL_READ:
                        self.state.pending_digital_reads.append(pin)
                    else:
                        self.state.pending_analog_reads.append(pin)
            else:
                logger.debug(
                    "Unknown pin subtopic for %s: %s", pin_str, subtopic
                )
        elif len(parts) == 3:
            value = self._parse_pin_value(topic_type, payload_str)
            if value is None:
                logger.warning(
                    "Invalid pin value topic=%s payload=%s",
                    "/".join(parts),
                    payload_str,
                )
                return
            command = (
                Command.CMD_DIGITAL_WRITE
                if topic_type == TOPIC_DIGITAL
                else Command.CMD_ANALOG_WRITE
            )
            await self.send_frame(
                command.value, struct.pack(">BB", pin, value)
            )

    def _parse_pin_identifier(self, pin_str: str) -> int:
        if pin_str.upper().startswith("A") and pin_str[1:].isdigit():
            return int(pin_str[1:])
        if pin_str.isdigit():
            return int(pin_str)
        return -1

    def _parse_pin_value(
        self, topic_type: str, payload_str: str
    ) -> Optional[int]:
        if not payload_str:
            return 0
        try:
            value = int(payload_str)
        except ValueError:
            return None

        if topic_type == TOPIC_DIGITAL and value in (0, 1):
            return value
        if topic_type == TOPIC_ANALOG and 0 <= value <= 255:
            return value
        return None

    async def _perform_file_operation(
        self, operation: str, filename: str, data: Optional[bytes] = None
    ) -> tuple[bool, Optional[bytes], str]:
        try:
            safe_path = await self._get_safe_path(filename)
            if not safe_path:
                return False, None, "unsafe_path"

            if operation == "write":
                if data is None:
                    return False, None, "missing_data"
                await asyncio.to_thread(self._write_file_sync, safe_path, data)
                logger.info("Wrote %d bytes to %s", len(data), safe_path)
                return True, None, "ok"

            if not await asyncio.to_thread(os.path.exists, safe_path):
                return False, None, "not_found"

            if operation == "read":
                content = await asyncio.to_thread(
                    self._read_file_sync,
                    safe_path,
                )
                logger.info("Read %d bytes from %s", len(content), safe_path)
                return True, content, "ok"

            if operation == "remove":
                await asyncio.to_thread(os.remove, safe_path)
                logger.info("Removed file %s", safe_path)
                return True, None, "ok"

        except OSError as e:
            logger.exception(
                "File operation %s failed for %s",
                operation,
                filename,
            )
            return False, None, str(e)
        return False, None, "unknown_operation"

    def _encode_status_reason(self, reason: Optional[str]) -> bytes:
        if not reason:
            return b""
        payload = reason.encode("utf-8", errors="ignore")
        if len(payload) > MAX_PAYLOAD_SIZE:
            return payload[:MAX_PAYLOAD_SIZE]
        return payload

    async def _get_safe_path(self, filename: str) -> Optional[str]:
        base_dir = os.path.abspath(self.state.file_system_root)
        try:
            os.makedirs(base_dir, exist_ok=True)
        except OSError:
            logger.exception(
                "Failed to create base directory for files: %s", base_dir
            )
            return None

        cleaned_filename = filename.lstrip("./\\").replace("../", "")
        safe_path = os.path.abspath(os.path.join(base_dir, cleaned_filename))

        if os.path.commonpath([safe_path, base_dir]) != base_dir:
            logger.warning(
                "Path traversal blocked. filename='%s', resolved='%s',"
                " base='%s'",
                filename,
                safe_path,
                base_dir,
            )
            return None
        return safe_path

    @staticmethod
    def _write_file_sync(path: str, data: bytes) -> None:
        directory = os.path.dirname(path)
        if directory:
            os.makedirs(directory, exist_ok=True)
        with open(path, "wb") as handle:
            handle.write(data)

    @staticmethod
    def _read_file_sync(path: str) -> bytes:
        with open(path, "rb") as handle:
            return handle.read()
