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
from typing import Awaitable, Callable, Dict, Optional, Tuple

import aio_mqtt
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

TOPIC_DIGITAL = "d"
TOPIC_ANALOG = "a"
TOPIC_CONSOLE = "console"
TOPIC_SH = "sh"
TOPIC_MAILBOX = "mailbox"
TOPIC_DATASTORE = "datastore"
TOPIC_FILE = "file"


class BridgeService:
    """Service façade used by the transport layer."""

    def __init__(self, config: RuntimeConfig, state: RuntimeState) -> None:
        self.config = config
        self.state = state
        self._serial_sender: Optional[SendFrameCallable] = None
        self._mcu_handlers: Dict[int, Callable[[bytes], Awaitable[None]]] = {
            Command.CMD_DIGITAL_READ_RESP.value:
                self._handle_digital_read_resp,
            Command.CMD_ANALOG_READ_RESP.value:
                self._handle_analog_read_resp,
            Command.CMD_GET_VERSION_RESP.value:
                self._handle_get_version_resp,
            Command.CMD_GET_FREE_MEMORY_RESP.value:
                self._handle_get_free_memory_resp,
            Status.ACK.value: self._handle_ack,
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
            await self.request_mcu_version()
        except Exception:
            logger.exception("Failed to request MCU version after reconnect")

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

    async def enqueue_mqtt(self, message: aio_mqtt.PublishableMessage) -> None:
        await self.state.mqtt_publish_queue.put(message)

    async def request_mcu_version(self) -> None:
        send_ok = await self.send_frame(Command.CMD_GET_VERSION.value, b"")
        if send_ok:
            self.state.mcu_version = None

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

        if handler:
            logger.debug("MCU > %s payload=%s", command_name, payload.hex())
            await handler(payload)
        elif command_id < 0x80:
            logger.warning("Unhandled MCU command %s", command_name)
            await self.send_frame(Status.NOT_IMPLEMENTED.value, b"")
        else:
            logger.debug("Ignoring MCU response %s", command_name)

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
        message = aio_mqtt.PublishableMessage(
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
        message = aio_mqtt.PublishableMessage(
            topic_name=topic,
            payload=str(value).encode("utf-8"),
        )
        await self.enqueue_mqtt(message)

    async def _handle_ack(self, _: bytes) -> None:
        logger.debug("MCU > ACK received")

    async def _handle_xoff(self, _: bytes) -> None:
        logger.warning("MCU > XOFF received, pausing console output.")
        self.state.mcu_is_paused = True

    async def _handle_xon(self, _: bytes) -> None:
        logger.info("MCU > XON received, resuming console output.")
        self.state.mcu_is_paused = False
        await self._flush_console_queue()

    async def _handle_console_write(self, payload: bytes) -> None:
        topic = f"{self.state.mqtt_topic_prefix}/{TOPIC_CONSOLE}/out"
        message = aio_mqtt.PublishableMessage(
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
            message = aio_mqtt.PublishableMessage(
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
        message = aio_mqtt.PublishableMessage(
            topic_name=topic,
            payload=value_bytes,
        )
        await self.enqueue_mqtt(message)

    async def _handle_mailbox_processed(self, payload: bytes) -> None:
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
            aio_mqtt.PublishableMessage(topic_name=topic_name, payload=body)
        )

    async def _handle_mailbox_push(self, payload: bytes) -> None:
        if len(payload) < 2:
            logger.warning("Malformed MAILBOX_PUSH payload: %s", payload.hex())
            return

        (msg_len,) = struct.unpack(">H", payload[:2])
        data = payload[2:2 + msg_len]
        if len(data) != msg_len:
            logger.warning(
                "MAILBOX_PUSH length mismatch. Expected %d bytes, got %d.",
                msg_len,
                len(data),
            )
            return

        topic = self.state.mailbox_incoming_topic or (
            f"{self.state.mqtt_topic_prefix}/{TOPIC_MAILBOX}/incoming"
        )
        await self.enqueue_mqtt(
            aio_mqtt.PublishableMessage(topic_name=topic, payload=data)
        )
        await self.send_frame(Status.ACK.value, b"")

    async def _handle_mailbox_available(self, _: bytes) -> None:
        count_payload = struct.pack(">B", len(self.state.mailbox_queue) & 0xFF)
        await self.send_frame(
            Command.CMD_MAILBOX_AVAILABLE_RESP.value,
            count_payload,
        )

    async def _handle_mailbox_read(self, _: bytes) -> None:
        message_payload = self.state.pop_mailbox_message()

        msg_len = len(message_payload)
        if msg_len > MAX_PAYLOAD_SIZE - 2:
            logger.warning(
                "Mailbox message too long (%d bytes), truncating.", msg_len
            )
            message_payload = message_payload[: MAX_PAYLOAD_SIZE - 2]
            msg_len = len(message_payload)

        response_payload = struct.pack(">H", msg_len) + message_payload
        await self.send_frame(
            Command.CMD_MAILBOX_READ_RESP.value,
            response_payload,
        )

        count_msg = aio_mqtt.PublishableMessage(
            topic_name=(
                f"{self.state.mqtt_topic_prefix}/{TOPIC_MAILBOX}/available"
            ),
            payload=str(len(self.state.mailbox_queue)).encode("utf-8"),
        )
        await self.enqueue_mqtt(count_msg)

    async def _handle_file_write(self, payload: bytes) -> None:
        if len(payload) < 3:
            logger.warning(
                "Invalid file write payload length: %d", len(payload)
            )
            return

        path_len = payload[0]
        cursor = 1
        if len(payload) < cursor + path_len + 2:
            logger.warning("Invalid file write payload: missing data section")
            return

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
            return

        success, _, reason = await self._perform_file_operation(
            "write", path, file_data
        )
        if success:
            await self.send_frame(Status.ACK.value, b"")
        else:
            await self.send_frame(
                Status.ERROR.value,
                self._encode_status_reason(reason or "write_failed"),
            )

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

    async def _handle_file_remove(self, payload: bytes) -> None:
        if len(payload) < 1:
            logger.warning(
                "Invalid file remove payload length: %d", len(payload)
            )
            return

        path_len = payload[0]
        if len(payload) < 1 + path_len:
            logger.warning("Invalid file remove payload: missing path bytes")
            return

        filename = payload[1:1 + path_len].decode("utf-8", errors="ignore")
        success, _, reason = await self._perform_file_operation(
            "remove", filename
        )
        if success:
            await self.send_frame(Status.ACK.value, b"")
        else:
            await self.send_frame(
                Status.ERROR.value,
                self._encode_status_reason(reason or "remove_failed"),
            )

    async def _handle_get_free_memory_resp(self, payload: bytes) -> None:
        if len(payload) != 2:
            logger.warning(
                "Malformed GET_FREE_MEMORY_RESP payload: %s", payload.hex()
            )
            return

        free_memory = int.from_bytes(payload, "big")
        topic = f"{self.state.mqtt_topic_prefix}/system/free_memory/value"
        message = aio_mqtt.PublishableMessage(
            topic_name=topic, payload=str(free_memory).encode("utf-8")
        )
        await self.enqueue_mqtt(message)

    async def _handle_get_version_resp(self, payload: bytes) -> None:
        if len(payload) != 2:
            logger.warning(
                "Malformed GET_VERSION_RESP payload: %s", payload.hex()
            )
            return

        major, minor = payload[0], payload[1]
        self.state.mcu_version = (major, minor)
        topic = f"{self.state.mqtt_topic_prefix}/system/version/value"
        message = aio_mqtt.PublishableMessage(
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
            aio_mqtt.PublishableMessage(
                topic_name=response_topic,
                payload=response_payload,
            )
        )

    async def _handle_process_poll(self, payload: bytes) -> None:
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
            return

        pid = int.from_bytes(payload, "big")
        (
            status_byte,
            exit_code,
            stdout_buffer,
            stderr_buffer,
            finished,
        ) = await self._collect_process_output(pid)

        (
            stdout_trim,
            stderr_trim,
            stdout_truncated,
            stderr_truncated,
        ) = self._trim_process_buffers(stdout_buffer, stderr_buffer)

        response_payload = (
            struct.pack(
                ">BBHH",
                status_byte,
                exit_code,
                len(stdout_trim),
                len(stderr_trim),
            )
            + stdout_trim
            + stderr_trim
        )

        await self.send_frame(
            Command.CMD_PROCESS_POLL_RESP.value, response_payload
        )

        await self._publish_process_poll_result(
            pid,
            status_byte,
            exit_code,
            stdout_trim,
            stderr_trim,
            stdout_truncated,
            stderr_truncated,
            finished,
        )

        if finished:
            logger.debug("Sent final output for finished process PID %d", pid)

    async def _handle_process_kill(self, payload: bytes) -> None:
        if len(payload) != 2:
            logger.warning(
                "Invalid PROCESS_KILL payload. Expected 2 bytes, got %d: %s",
                len(payload),
                payload.hex(),
            )
            return

        pid = int.from_bytes(payload, "big")

        async with self.state.process_lock:
            if pid in self.state.running_processes:
                proc_to_kill = self.state.running_processes[pid]
                try:
                    proc_to_kill.kill()
                    await asyncio.wait_for(proc_to_kill.wait(), timeout=0.5)
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

        await self.send_frame(Status.ACK.value, b"")

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
            return (Status.MALFORMED.value, b"", b"Empty command", None)

        if not self._is_command_allowed(command):
            error_msg = (
                f"Command '{command.split()[0]}' not allowed".encode("utf-8")
            )
            return (Status.ERROR.value, b"", error_msg, None)

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
            stdout_bytes, stderr_bytes = await asyncio.wait_for(
                proc.communicate(), timeout=self.state.process_timeout
            )
            status = Status.OK.value
        except asyncio.TimeoutError:
            proc.kill()
            try:
                stdout_bytes, stderr_bytes = await asyncio.wait_for(
                    proc.communicate(), timeout=1
                )
            except asyncio.TimeoutError:
                stdout_bytes, stderr_bytes = b"", b"Timeout waiting after kill"
            status = Status.TIMEOUT.value
        except Exception:
            logger.exception(
                "Unexpected error executing command '%s'", command
            )
            return (Status.ERROR.value, b"", b"Internal error", None)

        stdout_bytes = stdout_bytes or b""
        stderr_bytes = stderr_bytes or b""
        exit_code = proc.returncode
        return (status, stdout_bytes, stderr_bytes, exit_code)

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

        logger.info("Started async process '%s' with PID %d", command, pid)
        return pid

    async def _collect_process_output(
        self, pid: int
    ) -> Tuple[int, int, bytes, bytes, bool]:
        async with self.state.process_lock:
            proc = self.state.running_processes.get(pid)
            if not proc:
                return (Status.ERROR.value, 0xFF, b"", b"", False)

        async def _read_chunk(reader: Optional[asyncio.StreamReader]) -> bytes:
            if reader is None:
                return b""
            try:
                return await asyncio.wait_for(reader.read(1024), timeout=0.05)
            except asyncio.TimeoutError:
                return b""
            except (OSError, ValueError, BrokenPipeError):
                logger.debug(
                    "Error reading process pipe for PID %d", pid, exc_info=True
                )
                return b""

        stdout_buffer = await _read_chunk(proc.stdout)
        stderr_buffer = await _read_chunk(proc.stderr)

        process_finished = proc.returncode is not None
        exit_code = (
            proc.returncode
            if process_finished and proc.returncode is not None
            else 0xFF
        )

        if process_finished:
            try:
                stdout_rem, stderr_rem = await asyncio.wait_for(
                    proc.communicate(), timeout=0.2
                )
                stdout_buffer += stdout_rem or b""
                stderr_buffer += stderr_rem or b""
            except asyncio.TimeoutError:
                pass
            except Exception:
                logger.debug(
                    "Error collecting final output for PID %d",
                    pid,
                    exc_info=True,
                )

            async with self.state.process_lock:
                self.state.running_processes.pop(pid, None)
                logger.info(
                    "Async process %d finished with exit code %d",
                    pid,
                    exit_code,
                )

        status = Status.OK.value
        return (
            status,
            exit_code & 0xFF,
            stdout_buffer,
            stderr_buffer,
            process_finished,
        )

    def _trim_process_buffers(
        self, stdout_buffer: bytes, stderr_buffer: bytes
    ) -> Tuple[bytes, bytes, bool, bool]:
        max_payload = MAX_PAYLOAD_SIZE - 6
        stdout_trim = stdout_buffer[:max_payload]
        remaining = max_payload - len(stdout_trim)
        stderr_trim = stderr_buffer[:remaining]
        stdout_truncated = len(stdout_buffer) > len(stdout_trim)
        stderr_truncated = len(stderr_buffer) > len(stderr_trim)
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
        mqtt_payload = json.dumps(
            {
                "status": status_byte,
                "exit_code": exit_code,
                "stdout": stdout_trim.decode("utf-8", errors="ignore"),
                "stderr": stderr_trim.decode("utf-8", errors="ignore"),
                "stdout_truncated": stdout_truncated,
                "stderr_truncated": stderr_truncated,
                "finished": finished,
            }
        ).encode("utf-8")
        await self.enqueue_mqtt(
            aio_mqtt.PublishableMessage(
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
                        aio_mqtt.PublishableMessage(
                            topic_name=(
                                f"{self.state.mqtt_topic_prefix}/system/version/value"
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
                aio_mqtt.PublishableMessage(
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
            for chunk in self._iter_console_chunks(buffered):
                if not chunk:
                    continue
                await self.send_frame(Command.CMD_CONSOLE_WRITE.value, chunk)

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

        for chunk in chunks:
            if not chunk:
                continue
            await self.send_frame(Command.CMD_CONSOLE_WRITE.value, chunk)

    async def _handle_mqtt_datastore(
        self, action: str, key_parts: list[str], value_text: str
    ) -> None:
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
                aio_mqtt.PublishableMessage(
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
                    aio_mqtt.PublishableMessage(
                        topic_name=topic_name,
                        payload=cached_value.encode("utf-8"),
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
            aio_mqtt.PublishableMessage(
                topic_name=(
                    f"{self.state.mqtt_topic_prefix}/{TOPIC_MAILBOX}/available"
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
                aio_mqtt.PublishableMessage(
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
            ) = await self._collect_process_output(pid)

            (
                stdout_trim,
                stderr_trim,
                stdout_truncated,
                stderr_truncated,
            ) = self._trim_process_buffers(stdout_buffer, stderr_buffer)

            await self._publish_process_poll_result(
                pid,
                status_byte,
                exit_code,
                stdout_trim,
                stderr_trim,
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
            await self._handle_process_kill(pid_bytes)
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
            stdout, stderr = await asyncio.wait_for(
                proc.communicate(), timeout=self.state.process_timeout
            )
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
            aio_mqtt.PublishableMessage(
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
            logger.warning("Invalid pin identifier in topic: %s", pin_str)
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
                    "Invalid pin value topic=%s payload=%s", parts, payload_str
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
    ) -> tuple[bool, Optional[bytes], Optional[str]]:
        safe_path = await self._get_safe_path(filename)
        if not safe_path:
            logger.warning(
                "File operation blocked for unsafe path: %s", filename
            )
            return False, None, "unsafe_path"

        try:
            if operation == "write":
                if data is None:
                    logger.error(
                        "File write requested without data for %s", filename
                    )
                    return False, None, "missing_data"
                await asyncio.to_thread(self._write_file_sync, safe_path, data)
                logger.info("Wrote %d bytes to %s", len(data), safe_path)
                return True, None, None

            exists = await asyncio.to_thread(os.path.exists, safe_path)
            if not exists:
                logger.warning(
                    "File operation on non-existent file: %s", safe_path
                )
                return False, None, "not_found"

            if operation == "read":
                content = await asyncio.to_thread(
                    self._read_file_sync, safe_path
                )
                logger.info("Read %d bytes from %s", len(content), safe_path)
                return True, content, None

            if operation == "remove":
                await asyncio.to_thread(os.remove, safe_path)
                logger.info("Removed file %s", safe_path)
                return True, None, None

        except (ValueError, OSError):
            logger.exception(
                "File operation failed for %s (%s)", safe_path, operation
            )

        return False, None, "operation_failed"

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

