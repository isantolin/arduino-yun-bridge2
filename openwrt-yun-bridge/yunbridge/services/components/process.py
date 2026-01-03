"""Process management component for YunBridge."""

from __future__ import annotations

import asyncio
import base64
import json
import logging
import struct
import subprocess
from asyncio import StreamReader
from asyncio.subprocess import Process as AsyncioProcess
from contextlib import AsyncExitStack
from dataclasses import dataclass, field

import psutil

from ...common import encode_status_reason
from ...protocol.topics import Topic, topic_path
from ...mqtt.messages import QueuedPublish
from ...state.context import ManagedProcess, RuntimeState
from ...config.settings import RuntimeConfig
from ...policy import CommandValidationError, tokenize_shell_command
from .base import BridgeContext
from yunbridge.rpc import protocol
from yunbridge.rpc.protocol import (
    INVALID_ID_SENTINEL,
    PROCESS_DEFAULT_EXIT_CODE,
    UINT8_MASK,
    UINT16_MAX,
    Command,
    MAX_PAYLOAD_SIZE,
    ShellAction,
    Status,
)

logger = logging.getLogger("yunbridge.process")

_PROCESS_POLL_BUDGET = MAX_PAYLOAD_SIZE - 6


@dataclass(slots=True)
class ProcessOutputBatch:
    """Structured payload describing PROCESS_POLL results."""

    status_byte: int
    exit_code: int
    stdout_chunk: bytes
    stderr_chunk: bytes
    finished: bool
    stdout_truncated: bool
    stderr_truncated: bool


@dataclass(slots=True)
class ProcessComponent:
    """Encapsulates shell/process interactions for BridgeService."""

    config: RuntimeConfig
    state: RuntimeState
    ctx: BridgeContext
    _process_slots: asyncio.BoundedSemaphore | None = field(
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

        if not await self._try_acquire_process_slot():
            logger.warning(
                "Concurrent process limit reached (%d) for sync command",
                self.state.process_max_concurrent,
            )
            await self.ctx.send_frame(
                Status.ERROR.value,
                encode_status_reason("process_limit_reached"),
            )
            return

        await self.ctx.schedule_background(self._execute_sync_command(command))

    async def _execute_sync_command(self, command: str) -> None:
        async with AsyncExitStack() as stack:
            stack.callback(self._release_process_slot)
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
                await self.ctx.send_frame(Command.CMD_PROCESS_RUN_RESP.value, response)
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
        match pid:
            case protocol.INVALID_ID_SENTINEL:
                await self.ctx.send_frame(
                    Status.ERROR.value,
                    encode_status_reason("process_run_async_failed"),
                )
                await self._publish_run_async_error("process_run_async_failed")
                return
            case _:
                await self.ctx.send_frame(
                    Command.CMD_PROCESS_RUN_ASYNC_RESP.value,
                    struct.pack(protocol.UINT16_FORMAT, pid),
                )
                topic = topic_path(
                    self.state.mqtt_topic_prefix,
                    Topic.SHELL,
                    ShellAction.RUN_ASYNC,
                    protocol.MQTT_SUFFIX_RESPONSE,
                )
                await self.ctx.enqueue_mqtt(
                    QueuedPublish(
                        topic_name=topic,
                        payload=str(pid).encode(),
                    )
                )

    async def _publish_run_async_error(self, reason: str) -> None:
        topic = topic_path(
            self.state.mqtt_topic_prefix,
            Topic.SHELL,
            ShellAction.RUN_ASYNC,
            "error",
        )
        error_payload = json.dumps(
            {
                "status": "error",
                "reason": reason,
            }
        ).encode("utf-8")
        await self.ctx.enqueue_mqtt(
            QueuedPublish(topic_name=topic, payload=error_payload)
        )

    async def handle_poll(self, payload: bytes) -> bool:
        if len(payload) != 2:
            logger.warning("Invalid PROCESS_POLL payload: %s", payload.hex())
            await self.ctx.send_frame(
                Command.CMD_PROCESS_POLL_RESP.value,
                b"".join(
                    [
                        bytes([Status.MALFORMED.value, PROCESS_DEFAULT_EXIT_CODE]),
                        struct.pack(protocol.UINT16_FORMAT, 0),
                        struct.pack(protocol.UINT16_FORMAT, 0),
                    ]
                ),
            )
            return False

        pid = struct.unpack(protocol.UINT16_FORMAT, payload[:2])[0]
        batch = await self.collect_output(pid)

        response_payload = b"".join(
            [
                bytes([batch.status_byte, batch.exit_code]),
                struct.pack(protocol.UINT16_FORMAT, len(batch.stdout_chunk)),
                struct.pack(protocol.UINT16_FORMAT, len(batch.stderr_chunk)),
                batch.stdout_chunk,
                batch.stderr_chunk,
            ]
        )
        await self.ctx.send_frame(Command.CMD_PROCESS_POLL_RESP.value, response_payload)

        await self.publish_poll_result(pid, batch)
        if batch.finished:
            logger.debug("Sent final output for finished process PID %d", pid)
        return True

    async def handle_kill(self, payload: bytes, *, send_ack: bool = True) -> bool:
        if len(payload) != 2:
            logger.warning(
                "Invalid PROCESS_KILL payload. Expected 2 bytes, got %d: %s",
                len(payload),
                payload.hex(),
            )
            return False

        pid = struct.unpack(">H", payload[:2])[0]

        async with self.state.process_lock:
            slot = self.state.running_processes.get(pid)
            proc = slot.handle if slot is not None else None

        if proc is None:
            logger.warning("Attempted to kill non-existent PID: %d", pid)
            return send_ack

        try:
            await self._terminate_process_tree(proc)
            try:
                async with asyncio.timeout(0.5):
                    await proc.wait()
            except TimeoutError:
                logger.warning(
                    "Process PID %d did not terminate after kill signal.",
                    pid,
                )
            else:
                logger.info("Killed process with PID %d", pid)
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
                        proc.returncode if proc.returncode is not None else PROCESS_DEFAULT_EXIT_CODE
                    )
                    if slot.is_drained():
                        self.state.running_processes.pop(pid, None)
            if released_slot:
                self._release_process_slot()

        return send_ack

    async def run_sync(self, command: str) -> tuple[int, bytes, bytes, int | None]:
        try:
            tokens = self._prepare_command(command)
        except CommandValidationError as exc:
            logger.warning("Rejected command '%s': %s", command, exc)
            return Status.ERROR.value, b"", exc.message.encode("utf-8"), None

        try:
            proc = await asyncio.create_subprocess_exec(
                *tokens,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
            )
        except OSError as exc:
            return (
                Status.ERROR.value,
                b"",
                str(exc).encode("utf-8", errors="ignore"),
                None,
            )
        pid_hint = int(getattr(proc, "pid", 0) or 0)
        stdout_buffer = bytearray()
        stderr_buffer = bytearray()
        timed_out = False

        try:
            async with asyncio.TaskGroup() as tg:
                tg.create_task(
                    self._consume_stream(
                        pid_hint,
                        proc.stdout,
                        stdout_buffer,
                    )
                )
                tg.create_task(
                    self._consume_stream(
                        pid_hint,
                        proc.stderr,
                        stderr_buffer,
                    )
                )
                wait_task = tg.create_task(self._wait_for_sync_completion(proc, pid_hint))
        except Exception:
            logger.exception(
                "Unexpected error executing command '%s'",
                command,
            )
            await self._terminate_process_tree(proc)
            try:
                await proc.wait()
            except Exception:
                pass
            return Status.ERROR.value, b"", b"Internal error", None

        try:
            timed_out = bool(wait_task.result())
        except Exception:
            timed_out = False

        stdout_bytes = bytes(stdout_buffer)
        stderr_bytes = bytes(stderr_buffer)
        stdout_bytes, stdout_truncated = self._limit_sync_payload(stdout_bytes)
        stderr_bytes, stderr_truncated = self._limit_sync_payload(stderr_bytes)
        if stdout_truncated or stderr_truncated:
            logger.warning(
                "Synchronous command '%s' output truncated to %d bytes",
                command,
                self.state.process_output_limit,
            )
        status_value = Status.TIMEOUT.value if timed_out else Status.OK.value
        return (
            status_value,
            stdout_bytes,
            stderr_bytes,
            proc.returncode,
        )

    async def _wait_for_sync_completion(self, proc: AsyncioProcess, pid_hint: int) -> bool:
        timeout = self.state.process_timeout
        if timeout <= 0:
            await proc.wait()
            return False
        try:
            async with asyncio.timeout(timeout):
                await proc.wait()
            return False
        except TimeoutError:
            await self._terminate_process_tree(proc)
            try:
                async with asyncio.timeout(1):
                    await proc.wait()
            except TimeoutError:
                logger.warning(
                    "Synchronous process PID %d did not exit after kill",
                    pid_hint,
                )
            return True

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
            return INVALID_ID_SENTINEL

        async with AsyncExitStack() as stack:
            stack.callback(self._release_process_slot)

            pid = await self._allocate_pid()
            if pid == INVALID_ID_SENTINEL:
                return INVALID_ID_SENTINEL

            try:
                proc = await asyncio.create_subprocess_exec(
                    *tokens,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                )
            except OSError as exc:
                logger.warning(
                    "Failed to start async process '%s': %s",
                    command,
                    exc,
                )
                return INVALID_ID_SENTINEL
            except Exception:
                logger.exception(
                    "Unexpected error starting async process '%s'",
                    command,
                )
                return INVALID_ID_SENTINEL

            stack.pop_all()

        slot = ManagedProcess(pid=pid, command=command, handle=proc)
        async with self.state.process_lock:
            self.state.running_processes[pid] = slot

        await self.ctx.schedule_background(
            self._monitor_async_process(pid, proc),
            name=f"process-monitor-{pid}",
        )
        logger.info("Started async process '%s' with PID %d", command, pid)
        return pid

    def _prepare_command(self, command: str) -> tuple[str, ...]:
        tokens = self._tokenize_command(command)
        head = tokens[0]
        if not self.ctx.is_command_allowed(head):
            raise CommandValidationError(f"Command '{head}' not allowed")
        return tokens

    def _tokenize_command(self, command: str) -> tuple[str, ...]:
        return tokenize_shell_command(command)

    async def collect_output(self, pid: int) -> ProcessOutputBatch:
        async with self.state.process_lock:
            slot = self.state.running_processes.get(pid)
            proc = slot.handle if slot is not None else None

        if slot is None:
            logger.debug("PROCESS_POLL received for unknown PID %d", pid)
            return ProcessOutputBatch(
                status_byte=Status.ERROR.value,
                exit_code=PROCESS_DEFAULT_EXIT_CODE,
                stdout_chunk=b"",
                stderr_chunk=b"",
                finished=False,
                stdout_truncated=False,
                stderr_truncated=False,
            )

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
                return ProcessOutputBatch(
                    status_byte=Status.ERROR.value,
                    exit_code=PROCESS_DEFAULT_EXIT_CODE,
                    stdout_chunk=b"",
                    stderr_chunk=b"",
                    finished=False,
                    stdout_truncated=False,
                    stderr_truncated=False,
                )

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
                slot.exit_code = proc.returncode
                slot.handle = None
                finished_flag = True
                log_finished = True
                released_slot = True
            else:
                finished_flag = slot.handle is None

            exit_value = slot.exit_code if slot.exit_code is not None else PROCESS_DEFAULT_EXIT_CODE

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

        return ProcessOutputBatch(
            status_byte=Status.OK.value,
            exit_code=exit_value & UINT8_MASK,
            stdout_chunk=stdout_payload,
            stderr_chunk=stderr_payload,
            finished=finished_flag,
            stdout_truncated=stdout_truncated_limit,
            stderr_truncated=stderr_truncated_limit,
        )

    async def _consume_stream(
        self,
        pid: int,
        reader: StreamReader | None,
        buffer: bytearray,
        *,
        chunk_size: int = 4096,
    ) -> None:
        if reader is None:
            return
        while True:
            try:
                chunk = await reader.read(chunk_size)
            except (OSError, ValueError, BrokenPipeError, RuntimeError):
                logger.debug(
                    "Error reading process pipe for PID %d",
                    pid,
                    exc_info=True,
                )
                break
            except Exception:
                logger.debug(
                    "Error reading process pipe for PID %d",
                    pid,
                    exc_info=True,
                )
                break
            if not chunk:
                break
            buffer.extend(chunk)

    async def _read_process_pipes(
        self, pid: int, proc: AsyncioProcess
    ) -> tuple[bytes, bytes]:
        async with asyncio.TaskGroup() as tg:
            stdout_task = tg.create_task(self._read_stream_chunk(pid, proc.stdout))
            stderr_task = tg.create_task(self._read_stream_chunk(pid, proc.stderr))
        return stdout_task.result(), stderr_task.result()

    async def _drain_process_pipes(
        self, pid: int, proc: AsyncioProcess
    ) -> tuple[bytes, bytes]:
        async with asyncio.TaskGroup() as tg:
            stdout_task = tg.create_task(self._drain_stream(pid, proc.stdout))
            stderr_task = tg.create_task(self._drain_stream(pid, proc.stderr))
        return stdout_task.result(), stderr_task.result()

    async def _read_stream_chunk(
        self,
        pid: int,
        reader: StreamReader | None,
        *,
        size: int = 1024,
        timeout: float = 0.05,
    ) -> bytes:
        if reader is None:
            return b""
        chunk: bytes = b""
        try:
            if timeout > 0:
                chunk = await asyncio.wait_for(reader.read(size), timeout)
            else:
                chunk = await reader.read(size)
        except TimeoutError:
            return b""
        except (
            asyncio.IncompleteReadError,
            OSError,
            ValueError,
            BrokenPipeError,
            RuntimeError,
        ):
            logger.debug(
                "Error reading process pipe for PID %d",
                pid,
                exc_info=True,
            )
            return b""
        except Exception:
            logger.debug(
                "Unexpected error reading pipe for PID %d",
                pid,
                exc_info=True,
            )
            return b""
        return chunk or b""

    async def _drain_stream(
        self,
        pid: int,
        reader: StreamReader | None,
        *,
        chunk_size: int = 1024,
    ) -> bytes:
        if reader is None:
            return b""
        buffer = bytearray()
        await self._consume_stream(
            pid,
            reader,
            buffer,
            chunk_size=chunk_size,
        )
        return bytes(buffer)

    def trim_buffers(
        self,
        stdout_buffer: bytearray,
        stderr_buffer: bytearray,
    ) -> tuple[bytes, bytes, bool, bool]:
        return self._trim_process_buffers(stdout_buffer, stderr_buffer)

    def _trim_process_buffers(
        self,
        stdout_buffer: bytearray,
        stderr_buffer: bytearray,
    ) -> tuple[bytes, bytes, bool, bool]:
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
        batch: ProcessOutputBatch,
    ) -> None:
        topic = topic_path(
            self.state.mqtt_topic_prefix,
            Topic.SHELL,
            ShellAction.POLL,
            str(pid),
            protocol.MQTT_SUFFIX_RESPONSE,
        )
        payload = json.dumps(
            {
                "status": batch.status_byte,
                "exit_code": batch.exit_code,
                "stdout": batch.stdout_chunk.decode("utf-8", errors="replace"),
                "stderr": batch.stderr_chunk.decode("utf-8", errors="replace"),
                "stdout_base64": base64.b64encode(batch.stdout_chunk).decode("ascii"),
                "stderr_base64": base64.b64encode(batch.stderr_chunk).decode("ascii"),
                "stdout_truncated": batch.stdout_truncated,
                "stderr_truncated": batch.stderr_truncated,
                "finished": batch.finished,
            }
        ).encode("utf-8")
        message = QueuedPublish(
            topic_name=topic,
            payload=payload,
            content_type="application/json",
            message_expiry_interval=30,
            user_properties=(("bridge-process-pid", str(pid)),),
        )
        await self.ctx.enqueue_mqtt(message)

    async def _allocate_pid(self) -> int:
        async with self.state.process_lock:
            for _ in range(UINT16_MAX):
                candidate = self.state.next_pid & UINT16_MAX
                self.state.next_pid = (candidate + 1) & UINT16_MAX
                if self.state.next_pid == 0:
                    self.state.next_pid = 1
                if candidate == 0:
                    continue
                if candidate not in self.state.running_processes:
                    return candidate
        logger.error("No async process slots available; all PIDs in use")
        return INVALID_ID_SENTINEL

    async def _terminate_process_tree(self, proc: AsyncioProcess) -> None:
        if proc.returncode is not None:
            return
        pid_value = getattr(proc, "pid", None)
        if pid_value is None:
            proc.kill()
            return
        pid = int(pid_value)
        await asyncio.to_thread(self._kill_process_tree_sync, pid)
        proc.kill()

    async def _monitor_async_process(
        self,
        pid: int,
        proc: AsyncioProcess,
    ) -> None:
        try:
            await proc.wait()
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("Error while awaiting async process PID %d", pid)
            return
        await self._finalize_async_process(pid, proc)

    async def _finalize_async_process(
        self,
        pid: int,
        proc: AsyncioProcess,
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
        exit_value = proc.returncode if proc.returncode is not None else PROCESS_DEFAULT_EXIT_CODE
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
        return b"".join(
            [
                bytes([status & UINT8_MASK]),
                struct.pack(protocol.UINT16_FORMAT, len(stdout_trim)),
                stdout_trim,
                struct.pack(protocol.UINT16_FORMAT, len(stderr_trim)),
                stderr_trim,
            ]
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
        except TimeoutError:
            return False

    def _release_process_slot(self) -> None:
        guard = self._process_slots
        if guard is None:
            return
        try:
            guard.release()
        except ValueError:
            pass


__all__ = ["ProcessComponent", "ProcessOutputBatch"]
