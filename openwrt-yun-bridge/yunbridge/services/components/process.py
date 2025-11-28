"""Process management component for YunBridge."""
from __future__ import annotations

import asyncio
import base64
import json
import logging
from dataclasses import dataclass, field
from typing import Optional, Tuple

from ...common import encode_status_reason, pack_u16, unpack_u16
from ...protocol.topics import Topic, topic_path
from ...mqtt import PublishableMessage
from ...state.context import ManagedProcess, RuntimeState
from ...config.settings import RuntimeConfig
from ...policy import CommandValidationError, tokenize_shell_command
from .base import BridgeContext
from yunbridge.rpc.protocol import Command, MAX_PAYLOAD_SIZE, Status

logger = logging.getLogger("yunbridge.process")

try:
    import psutil  # type: ignore[import-first]
except ImportError:  # pragma: no cover - optional dependency on OpenWrt image
    psutil = None

_PROCESS_POLL_BUDGET = MAX_PAYLOAD_SIZE - 6


@dataclass(slots=True)
class ProcessComponent:
    """Encapsulates shell/process interactions for BridgeService."""

    config: RuntimeConfig
    state: RuntimeState
    ctx: BridgeContext
    _process_slots: Optional[asyncio.BoundedSemaphore] = field(
        init=False, repr=False, default=None
    )

    def __post_init__(self) -> None:
        limit = max(0, self.config.process_max_concurrent)
        if limit > 0:
            self._process_slots = asyncio.BoundedSemaphore(limit)
        else:
            self._process_slots = None

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
            except CommandValidationError as exc:
                await self.ctx.send_frame(
                    Status.ERROR.value,
                    encode_status_reason(exc.message),
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
        try:
            pid = await self.start_async(command)
        except CommandValidationError as exc:
            logger.warning("Rejected async command '%s': %s", command, exc)
            await self.ctx.send_frame(
                Status.ERROR.value,
                encode_status_reason("command_validation_failed"),
            )
            await self._publish_run_async_error("command_validation_failed")
            return
        if pid == 0xFFFF:
            await self.ctx.send_frame(
                Status.ERROR.value,
                encode_status_reason("process_run_async_failed"),
            )
            await self._publish_run_async_error("process_run_async_failed")
            return
        await self.ctx.send_frame(
            Command.CMD_PROCESS_RUN_ASYNC_RESP.value, pack_u16(pid)
        )
        topic = topic_path(
            self.state.mqtt_topic_prefix,
            Topic.SHELL,
            "run_async",
            "response",
        )
        await self.ctx.enqueue_mqtt(
            PublishableMessage(topic_name=topic, payload=str(pid).encode())
        )

    async def _publish_run_async_error(self, reason: str) -> None:
        topic = topic_path(
            self.state.mqtt_topic_prefix,
            Topic.SHELL,
            "run_async",
            "error",
        )
        error_payload = json.dumps(
            {
                "status": "error",
                "reason": reason,
            }
        ).encode("utf-8")
        await self.ctx.enqueue_mqtt(
            PublishableMessage(topic_name=topic, payload=error_payload)
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
            slot = self.state.running_processes.get(pid)
            proc = slot.handle if slot is not None else None

        if proc is None:
            logger.warning("Attempted to kill non-existent PID: %d", pid)
            return send_ack

        try:
            await self._terminate_process_tree(proc)
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
            released_slot = False
            async with self.state.process_lock:
                slot = self.state.running_processes.get(pid)
                if slot is not None:
                    if slot.handle is not None:
                        released_slot = True
                    slot.handle = None
                    slot.exit_code = (
                        proc.returncode
                        if proc.returncode is not None
                        else 0xFF
                    )
                    if slot.is_drained():
                        self.state.running_processes.pop(pid, None)
            if released_slot:
                self._release_process_slot()

        return send_ack

    async def run_sync(
        self, command: str
    ) -> Tuple[int, bytes, bytes, Optional[int]]:
        try:
            tokens = self._prepare_command(command)
        except CommandValidationError as exc:
            logger.warning("Rejected command '%s': %s", command, exc)
            return Status.ERROR.value, b"", exc.message.encode("utf-8"), None

        try:
            proc = await asyncio.create_subprocess_exec(
                *tokens,
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

        stdout_bytes = stdout_bytes or b""
        stderr_bytes = stderr_bytes or b""
        stdout_bytes, stdout_truncated = self._limit_sync_payload(stdout_bytes)
        stderr_bytes, stderr_truncated = self._limit_sync_payload(stderr_bytes)
        if stdout_truncated or stderr_truncated:
            logger.warning(
                "Synchronous command '%s' output truncated to %d bytes",
                command,
                self.state.process_output_limit,
            )
        return (
            Status.OK.value,
            stdout_bytes,
            stderr_bytes,
            proc.returncode,
        )

    async def start_async(self, command: str) -> int:
        try:
            tokens = self._prepare_command(command)
        except CommandValidationError as exc:
            logger.warning("Rejected async command '%s': %s", command, exc)
            raise
        if not await self._try_acquire_process_slot():
            logger.warning(
                "Concurrent process limit reached (%d)",
                self.state.process_max_concurrent,
            )
            return 0xFFFF

        pid = await self._allocate_pid()
        if pid == 0xFFFF:
            self._release_process_slot()
            return 0xFFFF

        try:
            proc = await asyncio.create_subprocess_exec(
                *tokens,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
        except OSError as exc:
            logger.warning(
                "Failed to start async process '%s': %s",
                command,
                exc,
            )
            self._release_process_slot()
            return 0xFFFF
        except Exception:
            logger.exception(
                "Unexpected error starting async process '%s'",
                command,
            )
            self._release_process_slot()
            return 0xFFFF

        slot = ManagedProcess(pid=pid, command=command, handle=proc)
        async with self.state.process_lock:
            self.state.running_processes[pid] = slot

        self.ctx.schedule_background(
            self._monitor_async_process(pid, proc),
            name=f"process-monitor-{pid}",
        )
        logger.info("Started async process '%s' with PID %d", command, pid)
        return pid

    def _prepare_command(self, command: str) -> Tuple[str, ...]:
        tokens = self._tokenize_command(command)
        head = tokens[0]
        if not self.ctx.is_command_allowed(head):
            raise CommandValidationError(
                f"Command '{head}' not allowed"
            )
        return tokens

    def _tokenize_command(self, command: str) -> Tuple[str, ...]:
        return tokenize_shell_command(command)

    async def collect_output(
        self, pid: int
    ) -> Tuple[int, int, bytes, bytes, bool, bool, bool]:
        async with self.state.process_lock:
            slot = self.state.running_processes.get(pid)
            proc = slot.handle if slot is not None else None

        if slot is None:
            logger.debug("PROCESS_POLL received for unknown PID %d", pid)
            return Status.ERROR.value, 0xFF, b"", b"", False, False, False

        stdout_chunk = b""
        stderr_chunk = b""
        stdout_truncated_limit = False
        stderr_truncated_limit = False
        finished_flag = proc is None
        log_finished = False

        if proc is not None:
            async with slot.io_lock:
                chunk_out, chunk_err = await self._read_process_pipes(
                    pid,
                    proc,
                )
                if chunk_out:
                    stdout_chunk += chunk_out
                if chunk_err:
                    stderr_chunk += chunk_err
                if proc.returncode is not None:
                    finished_flag = True
                    (
                        extra_stdout,
                        extra_stderr,
                    ) = await self._drain_process_pipes(
                        pid,
                        proc,
                    )
                    if extra_stdout:
                        stdout_chunk += extra_stdout
                    if extra_stderr:
                        stderr_chunk += extra_stderr

        async with self.state.process_lock:
            slot = self.state.running_processes.get(pid)
            if slot is None:
                return Status.ERROR.value, 0xFF, b"", b"", False, False, False

            if stdout_chunk or stderr_chunk:
                trunc_out, trunc_err = slot.append_output(
                    stdout_chunk,
                    stderr_chunk,
                    limit=self.state.process_output_limit,
                )
                stdout_truncated_limit |= trunc_out
                stderr_truncated_limit |= trunc_err

            (
                stdout_payload,
                stderr_payload,
                payload_trunc_out,
                payload_trunc_err,
            ) = slot.pop_payload(_PROCESS_POLL_BUDGET)
            stdout_truncated_limit |= payload_trunc_out
            stderr_truncated_limit |= payload_trunc_err

            released_slot = False
            if proc is not None and proc.returncode is not None:
                slot.exit_code = (
                    proc.returncode if proc.returncode is not None else 0
                )
                slot.handle = None
                finished_flag = True
                log_finished = True
                released_slot = True
            else:
                finished_flag = slot.handle is None

            exit_value = (
                slot.exit_code if slot.exit_code is not None else 0xFF
            )

            if slot.handle is None and slot.is_drained():
                self.state.running_processes.pop(pid, None)

        if released_slot:
            self._release_process_slot()

        if log_finished:
            logger.info(
                "Async process %d finished with exit code %d",
                pid,
                exit_value,
            )

        return (
            Status.OK.value,
            exit_value & 0xFF,
            stdout_payload,
            stderr_payload,
            finished_flag,
            stdout_truncated_limit,
            stderr_truncated_limit,
        )

    async def _read_process_pipes(
        self, pid: int, proc: asyncio.subprocess.Process
    ) -> tuple[bytes, bytes]:
        async with asyncio.TaskGroup() as tg:
            stdout_task = tg.create_task(
                self._read_stream_chunk(pid, proc.stdout)
            )
            stderr_task = tg.create_task(
                self._read_stream_chunk(pid, proc.stderr)
            )
        return stdout_task.result(), stderr_task.result()

    async def _drain_process_pipes(
        self, pid: int, proc: asyncio.subprocess.Process
    ) -> tuple[bytes, bytes]:
        async with asyncio.TaskGroup() as tg:
            stdout_task = tg.create_task(self._drain_stream(pid, proc.stdout))
            stderr_task = tg.create_task(self._drain_stream(pid, proc.stderr))
        return stdout_task.result(), stderr_task.result()

    async def _read_stream_chunk(
        self,
        pid: int,
        reader: Optional[asyncio.StreamReader],
        *,
        size: int = 1024,
        timeout: float = 0.05,
    ) -> bytes:
        if reader is None:
            return b""
        try:
            async with asyncio.timeout(timeout):
                return await reader.read(size)
        except asyncio.TimeoutError:
            return b""
        except (OSError, ValueError, BrokenPipeError):
            logger.debug(
                "Error reading process pipe for PID %d", pid, exc_info=True
            )
            return b""

    async def _drain_stream(
        self,
        pid: int,
        reader: Optional[asyncio.StreamReader],
    ) -> bytes:
        if reader is None:
            return b""
        chunks = bytearray()
        while True:
            chunk = await self._read_stream_chunk(pid, reader)
            if not chunk:
                break
            chunks.extend(chunk)
            if len(chunk) < 1024:
                break
        return bytes(chunks)

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
        max_payload = _PROCESS_POLL_BUDGET
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
        topic = topic_path(
            self.state.mqtt_topic_prefix,
            Topic.SHELL,
            "poll",
            str(pid),
            "response",
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

    async def _terminate_process_tree(
        self, proc: asyncio.subprocess.Process
    ) -> None:
        if proc.returncode is not None:
            return
        if psutil is None or proc.pid is None:
            proc.kill()
            return
        pid = proc.pid
        await asyncio.to_thread(self._kill_process_tree_sync, pid)
        proc.kill()

    async def _monitor_async_process(
        self,
        pid: int,
        proc: asyncio.subprocess.Process,
    ) -> None:
        try:
            await proc.wait()
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception(
                "Error while awaiting async process PID %d", pid
            )
            return
        await self._finalize_async_process(pid, proc)

    async def _finalize_async_process(
        self,
        pid: int,
        proc: asyncio.subprocess.Process,
    ) -> None:
        async with self.state.process_lock:
            slot = self.state.running_processes.get(pid)
        if slot is None:
            self._release_process_slot()
            return

        async with slot.io_lock:
            stdout_tail, stderr_tail = await self._drain_process_pipes(
                pid,
                proc,
            )

        release_slot = False
        exit_value = proc.returncode if proc.returncode is not None else 0xFF
        async with self.state.process_lock:
            current_slot = self.state.running_processes.get(pid)
            if current_slot is None or current_slot is not slot:
                return
            if current_slot.handle is not proc:
                return
            if stdout_tail or stderr_tail:
                current_slot.append_output(
                    stdout_tail,
                    stderr_tail,
                    limit=self.state.process_output_limit,
                )
            current_slot.exit_code = exit_value
            current_slot.handle = None
            if current_slot.is_drained():
                self.state.running_processes.pop(pid, None)
            release_slot = True

        if release_slot:
            self._release_process_slot()
            logger.info(
                "Async process %d finished with exit code %d",
                pid,
                exit_value,
            )

    @staticmethod
    def _kill_process_tree_sync(pid: int) -> None:
        if psutil is None:
            return
        try:
            process = psutil.Process(pid)
        except psutil.Error:
            return
        try:
            children = process.children(recursive=True)
        except psutil.Error:
            children = []
        for child in children:
            try:
                child.kill()
            except psutil.Error:
                continue
        try:
            process.kill()
        except psutil.Error:
            pass

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

    def _limit_sync_payload(self, payload: bytes) -> tuple[bytes, bool]:
        limit = self.state.process_output_limit
        if limit <= 0 or len(payload) <= limit:
            return payload, False
        return payload[-limit:], True

    async def _try_acquire_process_slot(self) -> bool:
        guard = self._process_slots
        if guard is None:
            return True
        try:
            await asyncio.wait_for(guard.acquire(), timeout=0)
            return True
        except asyncio.TimeoutError:
            return False

    def _release_process_slot(self) -> None:
        guard = self._process_slots
        if guard is None:
            return
        try:
            guard.release()
        except ValueError:
            pass


__all__ = ["ProcessComponent"]
