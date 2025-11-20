"""Process management component for YunBridge."""
from __future__ import annotations

import asyncio
import base64
import json
import logging
from dataclasses import dataclass
from typing import Optional, Tuple

from ...common import encode_status_reason, pack_u16, unpack_u16
from ...const import (
    TOPIC_SHELL,
)
from ...mqtt import PublishableMessage
from ...state.context import RuntimeState
from ...config.settings import RuntimeConfig
from .base import BridgeContext
from yunbridge.rpc.protocol import Command, MAX_PAYLOAD_SIZE, Status

logger = logging.getLogger("yunbridge.process")


@dataclass(slots=True)
class ProcessComponent:
    """Encapsulates shell/process interactions for BridgeService."""

    config: RuntimeConfig
    state: RuntimeState
    ctx: BridgeContext

    async def handle_run(self, payload: bytes) -> None:
        command = payload.decode("utf-8", errors="ignore")

        async def _execute() -> None:
            try:
                (
                    status,
                    stdout_bytes,
                    stderr_bytes,
                    exit_code,
                ) = await self.run_sync(command)
                response = self._build_sync_response(
                    status,
                    stdout_bytes,
                    stderr_bytes,
                )
                await self.ctx.send_frame(
                    Command.CMD_PROCESS_RUN_RESP.value, response
                )
                logger.debug(
                    "Sent PROCESS_RUN_RESP status=%d exit=%s",
                    status,
                    exit_code,
                )
            except Exception:
                logger.exception(
                    "Failed to execute synchronous process command '%s'",
                    command,
                )
                await self.ctx.send_frame(
                    Status.ERROR.value,
                    b"process_run_internal_error",
                )

        self.ctx.schedule_background(_execute())

    async def handle_run_async(self, payload: bytes) -> None:
        command = payload.decode("utf-8", errors="ignore")
        pid = await self.start_async(command)
        if pid == 0xFFFF:
            await self.ctx.send_frame(
                Status.ERROR.value,
                encode_status_reason("process_run_async_failed"),
            )
            topic = (
                f"{self.state.mqtt_topic_prefix}/{TOPIC_SHELL}/"
                "run_async/error"
            )
            error_payload = json.dumps(
                {
                    "status": "error",
                    "reason": "process_run_async_failed",
                }
            ).encode("utf-8")
            await self.ctx.enqueue_mqtt(
                PublishableMessage(topic_name=topic, payload=error_payload)
            )
            return
        await self.ctx.send_frame(
            Command.CMD_PROCESS_RUN_ASYNC_RESP.value, pack_u16(pid)
        )
        topic = (
            f"{self.state.mqtt_topic_prefix}/{TOPIC_SHELL}/"
            "run_async/response"
        )
        await self.ctx.enqueue_mqtt(
            PublishableMessage(topic_name=topic, payload=str(pid).encode())
        )

    async def handle_poll(self, payload: bytes) -> bool:
        if len(payload) != 2:
            logger.warning(
                "Invalid PROCESS_POLL payload: %s", payload.hex()
            )
            await self.ctx.send_frame(
                Command.CMD_PROCESS_POLL_RESP.value,
                bytes(
                    [Status.MALFORMED.value, 0xFF]
                ) + pack_u16(0) + pack_u16(0),
            )
            return False

        pid = unpack_u16(payload)
        (
            status_byte,
            exit_code,
            stdout_chunk,
            stderr_chunk,
            finished,
            stdout_truncated,
            stderr_truncated,
        ) = await self.collect_output(pid)

        response_payload = (
            bytes([status_byte, exit_code])
            + pack_u16(len(stdout_chunk))
            + pack_u16(len(stderr_chunk))
            + stdout_chunk
            + stderr_chunk
        )
        await self.ctx.send_frame(
            Command.CMD_PROCESS_POLL_RESP.value, response_payload
        )

        await self.publish_poll_result(
            pid,
            status_byte,
            exit_code,
            stdout_chunk,
            stderr_chunk,
            stdout_truncated,
            stderr_truncated,
            finished,
        )
        if finished:
            logger.debug("Sent final output for finished process PID %d", pid)
        return True

    async def handle_kill(
        self, payload: bytes, *, send_ack: bool = True
    ) -> bool:
        if len(payload) != 2:
            logger.warning(
                "Invalid PROCESS_KILL payload. Expected 2 bytes, got %d: %s",
                len(payload),
                payload.hex(),
            )
            return False

        pid = unpack_u16(payload)

        async with self.state.process_lock:
            proc = self.state.running_processes.get(pid)
            if proc is None:
                logger.warning("Attempted to kill non-existent PID: %d", pid)
                return send_ack

            try:
                proc.kill()
                async with asyncio.timeout(0.5):
                    await proc.wait()
                logger.info("Killed process with PID %d", pid)
            except asyncio.TimeoutError:
                logger.warning(
                    "Process PID %d did not terminate after kill signal.", pid
                )
            except ProcessLookupError:
                logger.info("Process PID %d already exited before kill.", pid)
            except Exception:
                logger.exception("Error killing process PID %d", pid)
            finally:
                self.state.running_processes.pop(pid, None)

        return send_ack

    async def run_sync(
        self, command: str
    ) -> Tuple[int, bytes, bytes, Optional[int]]:
        if not command.strip():
            return Status.MALFORMED.value, b"", b"Empty command", None

        if not self.ctx.is_command_allowed(command):
            logger.warning("Rejected blocked command: '%s'", command)
            first = command.split()[0] if command else ""
            return (
                Status.ERROR.value,
                b"",
                f"Command '{first}' not allowed".encode("utf-8"),
                None,
            )

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

        stdout_bytes: bytes
        stderr_bytes: bytes
        try:
            async with asyncio.TaskGroup() as tg:
                comm_task = tg.create_task(proc.communicate())
                try:
                    async with asyncio.timeout(self.state.process_timeout):
                        await comm_task
                except asyncio.TimeoutError:
                    proc.kill()
                    async with asyncio.timeout(1):
                        await comm_task
                    if not comm_task.done():
                        return (
                            Status.TIMEOUT.value,
                            b"",
                            b"Timeout after kill",
                            None,
                        )
                    stdout_bytes, stderr_bytes = comm_task.result()
                    return (
                        Status.TIMEOUT.value,
                        stdout_bytes,
                        stderr_bytes,
                        proc.returncode,
                    )
            stdout_bytes, stderr_bytes = comm_task.result()
        except Exception:
            logger.exception(
                "Unexpected error executing command '%s'",
                command,
            )
            return Status.ERROR.value, b"", b"Internal error", None

        return (
            Status.OK.value,
            stdout_bytes or b"",
            stderr_bytes or b"",
            proc.returncode,
        )

    async def start_async(self, command: str) -> int:
        if not command.strip():
            return 0xFFFF
        if not self.ctx.is_command_allowed(command):
            logger.warning(
                "Rejected async command due to policy: '%s'",
                command,
            )
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
                "Failed to start async process '%s': %s",
                command,
                exc,
            )
            return 0xFFFF
        except Exception:
            logger.exception(
                "Unexpected error starting async process '%s'",
                command,
            )
            return 0xFFFF

        async with self.state.process_lock:
            self.state.running_processes[pid] = proc
            self.state.process_stdout_buffer[pid] = bytearray()
            self.state.process_stderr_buffer[pid] = bytearray()
            self.state.process_exit_codes.pop(pid, None)

        logger.info("Started async process '%s' with PID %d", command, pid)
        return pid

    async def collect_output(
        self, pid: int
    ) -> Tuple[int, int, bytes, bytes, bool, bool, bool]:
        async def _read_chunk(
            reader: Optional[asyncio.StreamReader],
            *,
            size: int = 1024,
        ) -> bytes:
            if reader is None:
                return b""
            try:
                async with asyncio.timeout(0.05):
                    return await reader.read(size)
            except asyncio.TimeoutError:
                return b""
            except (OSError, ValueError, BrokenPipeError):
                logger.debug(
                    "Error reading process pipe for PID %d", pid, exc_info=True
                )
                return b""

        async def _drain(reader: Optional[asyncio.StreamReader]) -> bytes:
            if reader is None:
                return b""
            chunks = bytearray()
            while True:
                chunk = await _read_chunk(reader)
                if not chunk:
                    break
                chunks.extend(chunk)
                if len(chunk) < 1024:
                    break
            return bytes(chunks)

        async with self.state.process_lock:
            proc = self.state.running_processes.get(pid)
            stdout_buf = self.state.process_stdout_buffer.setdefault(
                pid, bytearray()
            )
            stderr_buf = self.state.process_stderr_buffer.setdefault(
                pid, bytearray()
            )
            cached_exit = self.state.process_exit_codes.get(pid)

        if proc is None:
            if cached_exit is None:
                logger.debug("PROCESS_POLL received for unknown PID %d", pid)
                return Status.ERROR.value, 0xFF, b"", b"", False, False, False

            async with self.state.process_lock:
                stdout_buf = self.state.process_stdout_buffer.setdefault(
                    pid, bytearray()
                )
                stderr_buf = self.state.process_stderr_buffer.setdefault(
                    pid, bytearray()
                )
                stdout_chunk, stderr_chunk, truncated_out, truncated_err = (
                    self._trim_process_buffers(stdout_buf, stderr_buf)
                )
                empty = not stdout_buf and not stderr_buf
                exit_value = self.state.process_exit_codes.get(pid, 0xFF)
                if empty:
                    self.state.process_stdout_buffer.pop(pid, None)
                    self.state.process_stderr_buffer.pop(pid, None)
                    self.state.process_exit_codes.pop(pid, None)

            return (
                Status.OK.value,
                exit_value & 0xFF,
                stdout_chunk,
                stderr_chunk,
                True,
                truncated_out,
                truncated_err,
            )

        stdout_collected = bytearray()
        stderr_collected = bytearray()

        stdout_chunk = await _read_chunk(proc.stdout)
        if stdout_chunk:
            stdout_collected.extend(stdout_chunk)

        stderr_chunk = await _read_chunk(proc.stderr)
        if stderr_chunk:
            stderr_collected.extend(stderr_chunk)

        finished = proc.returncode is not None

        if finished:
            stdout_remaining = await _drain(proc.stdout)
            if stdout_remaining:
                stdout_collected.extend(stdout_remaining)
            stderr_remaining = await _drain(proc.stderr)
            if stderr_remaining:
                stderr_collected.extend(stderr_remaining)

        log_finished = False

        async with self.state.process_lock:
            stdout_buf = self.state.process_stdout_buffer.setdefault(
                pid, bytearray()
            )
            stderr_buf = self.state.process_stderr_buffer.setdefault(
                pid, bytearray()
            )

            if stdout_collected:
                stdout_buf.extend(stdout_collected)
            if stderr_collected:
                stderr_buf.extend(stderr_collected)

            if finished:
                exit_value = (
                    proc.returncode if proc.returncode is not None else 0
                )
                if pid in self.state.running_processes:
                    self.state.running_processes.pop(pid, None)
                    log_finished = True
                self.state.process_exit_codes[pid] = exit_value
            else:
                exit_value = 0xFF

            stdout_chunk, stderr_chunk, truncated_out, truncated_err = (
                self._trim_process_buffers(stdout_buf, stderr_buf)
            )
            empty = not stdout_buf and not stderr_buf
            if finished and empty:
                self.state.process_stdout_buffer.pop(pid, None)
                self.state.process_stderr_buffer.pop(pid, None)
                self.state.process_exit_codes.pop(pid, None)

        if log_finished:
            logger.info(
                "Async process %d finished with exit code %d",
                pid,
                exit_value,
            )

        return (
            Status.OK.value,
            exit_value & 0xFF,
            stdout_chunk,
            stderr_chunk,
            finished,
            truncated_out,
            truncated_err,
        )

    def trim_buffers(
        self,
        stdout_buffer: bytearray,
        stderr_buffer: bytearray,
    ) -> Tuple[bytes, bytes, bool, bool]:
        return self._trim_process_buffers(stdout_buffer, stderr_buffer)

    def _trim_process_buffers(
        self,
        stdout_buffer: bytearray,
        stderr_buffer: bytearray,
    ) -> Tuple[bytes, bytes, bool, bool]:
        max_payload = MAX_PAYLOAD_SIZE - 6
        stdout_len = min(len(stdout_buffer), max_payload)
        stdout_chunk = bytes(stdout_buffer[:stdout_len])
        del stdout_buffer[:stdout_len]

        remaining = max_payload - len(stdout_chunk)
        stderr_len = min(len(stderr_buffer), remaining)
        stderr_chunk = bytes(stderr_buffer[:stderr_len])
        del stderr_buffer[:stderr_len]

        truncated_out = len(stdout_buffer) > 0
        truncated_err = len(stderr_buffer) > 0
        return stdout_chunk, stderr_chunk, truncated_out, truncated_err

    async def publish_poll_result(
        self,
        pid: int,
        status_byte: int,
        exit_code: int,
        stdout_chunk: bytes,
        stderr_chunk: bytes,
        stdout_truncated: bool,
        stderr_truncated: bool,
        finished: bool,
    ) -> None:
        topic = (
            f"{self.state.mqtt_topic_prefix}/{TOPIC_SHELL}/"
            f"poll/{pid}/response"
        )
        message = (
            PublishableMessage(topic_name=topic, payload=b"")
            .with_content_type("application/json")
            .with_message_expiry(30)
            .with_user_property("bridge-process-pid", str(pid))
        )
        payload = json.dumps(
            {
                "status": status_byte,
                "exit_code": exit_code,
                "stdout": stdout_chunk.decode("utf-8", errors="replace"),
                "stderr": stderr_chunk.decode("utf-8", errors="replace"),
                "stdout_base64": base64.b64encode(stdout_chunk).decode(
                    "ascii"
                ),
                "stderr_base64": base64.b64encode(stderr_chunk).decode(
                    "ascii"
                ),
                "stdout_truncated": stdout_truncated,
                "stderr_truncated": stderr_truncated,
                "finished": finished,
            }
        ).encode("utf-8")
        await self.ctx.enqueue_mqtt(message.with_payload(payload))

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

    def _build_sync_response(
        self, status: int, stdout_bytes: bytes, stderr_bytes: bytes
    ) -> bytes:
        max_payload = MAX_PAYLOAD_SIZE - 5
        stdout_trim = stdout_bytes[:max_payload]
        remaining = max_payload - len(stdout_trim)
        stderr_trim = stderr_bytes[:remaining]
        return (
            bytes([status & 0xFF])
            + pack_u16(len(stdout_trim))
            + stdout_trim
            + pack_u16(len(stderr_trim))
            + stderr_trim
        )


__all__ = ["ProcessComponent"]
