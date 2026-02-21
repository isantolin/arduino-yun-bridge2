"""Process management component for McuBridge."""

from __future__ import annotations

import asyncio
import base64
import logging
import subprocess
from asyncio import StreamReader
from asyncio.subprocess import Process
from contextlib import AsyncExitStack

import msgspec
import psutil
from construct import ConstructError
from mcubridge.protocol import protocol
from mcubridge.protocol.protocol import (
    INVALID_ID_SENTINEL,
    MAX_PAYLOAD_SIZE,
    PROCESS_DEFAULT_EXIT_CODE,
    UINT8_MASK,
    UINT16_MAX,
    Command,
    ShellAction,
    Status,
)
from mcubridge.protocol.structures import (
    ProcessKillPacket,
    ProcessOutputBatch,
    ProcessPollPacket,
    ProcessPollResponsePacket,
    ProcessRunAsyncPacket,
    ProcessRunAsyncResponsePacket,
    ProcessRunPacket,
    ProcessRunResponsePacket,
    UINT16_STRUCT,
)

from ..config.const import MQTT_EXPIRY_SHELL, PROCESS_KILL_WAIT_TIMEOUT, PROCESS_SYNC_KILL_WAIT_TIMEOUT
from ..config.settings import RuntimeConfig
from ..policy import CommandValidationError, tokenize_shell_command
from ..protocol.encoding import encode_status_reason
from ..protocol.topics import Topic, topic_path
from ..state.context import (
    ManagedProcess,
    RuntimeState,
    PROCESS_STATE_FINISHED,
    PROCESS_STATE_ZOMBIE,
)
from .base import BridgeContext

logger = logging.getLogger("mcubridge.process")

_PROCESS_POLL_BUDGET = protocol.MAX_PAYLOAD_SIZE - 6


class ProcessComponent:
    """Encapsulates shell/process interactions for BridgeService."""

    def __init__(
        self,
        config: RuntimeConfig,
        state: RuntimeState,
        ctx: BridgeContext,
    ) -> None:
        self.config = config
        self.state = state
        self.ctx = ctx
        limit = max(0, self.config.process_max_concurrent)
        if limit > 0:
            self._process_slots = asyncio.BoundedSemaphore(limit)
        else:
            self._process_slots = None

    def _prepare_command(self, command_str: str) -> tuple[str, list[str]]:
        """Tokenize command and check allowed policy."""
        tokens = list(tokenize_shell_command(command_str))
        if not tokens or not self.state.allowed_policy.is_allowed(tokens[0]):
            raise CommandValidationError(f"Command '{tokens[0] if tokens else ''}' not allowed")
        return command_str, tokens

    async def handle_run(self, payload: bytes) -> None:
        try:
            packet = ProcessRunPacket.decode(payload)
            command_str = packet.command
            command, tokens = self._prepare_command(command_str)
        except (ConstructError, ValueError) as e:
            logger.warning("Malformed PROCESS_RUN payload: %s", e)
            await self.ctx.send_frame(
                Status.MALFORMED.value,
                encode_status_reason(protocol.STATUS_REASON_COMMAND_VALIDATION_FAILED),
            )
            return
        except CommandValidationError as exc:
            logger.warning("Rejected sync command: %s", exc)
            await self.ctx.send_frame(
                Status.ERROR.value,
                encode_status_reason(protocol.STATUS_REASON_COMMAND_VALIDATION_FAILED),
            )
            return

        if not await self._try_acquire_process_slot():
            logger.warning(
                "Concurrent process limit reached (%d) for sync command",
                self.state.process_max_concurrent,
            )
            await self.ctx.send_frame(
                Status.ERROR.value,
                encode_status_reason(protocol.STATUS_REASON_PROCESS_LIMIT_REACHED),
            )
            return

        await self.ctx.schedule_background(self._execute_sync_command(command, tokens))

    async def _execute_sync_command(self, command: str, tokens: list[str]) -> None:
        async with AsyncExitStack() as stack:
            stack.callback(self._release_process_slot)
            try:
                (
                    status,
                    stdout_bytes,
                    stderr_bytes,
                    exit_code,
                ) = await self.run_sync(command, tokens)

                # [SIL-2] Use structured packet
                response = ProcessRunResponsePacket(
                    status=status,
                    stdout=stdout_bytes,
                    stderr=stderr_bytes,
                    exit_code=exit_code if exit_code is not None else PROCESS_DEFAULT_EXIT_CODE,
                ).encode()

                await self.ctx.send_frame(Command.CMD_PROCESS_RUN_RESP.value, response)
                logger.debug(
                    "Sent PROCESS_RUN_RESP status=%d exit=%s",
                    status,
                    exit_code,
                )
            except (OSError, ValueError) as e:
                logger.error(
                    "System error executing process command '%s': %s",
                    command,
                    e,
                )
                await self.ctx.send_frame(
                    Status.ERROR.value,
                    encode_status_reason(protocol.STATUS_REASON_PROCESS_RUN_INTERNAL_ERROR),
                )

    async def handle_run_async(self, payload: bytes) -> None:
        try:
            packet = ProcessRunAsyncPacket.decode(payload)
            command, tokens = self._prepare_command(packet.command)
            pid = await self.start_async(command, tokens)
        except (ConstructError, ValueError):
            logger.warning("Malformed PROCESS_RUN_ASYNC payload")
            await self.ctx.send_frame(
                Status.MALFORMED.value,
                encode_status_reason(protocol.STATUS_REASON_COMMAND_VALIDATION_FAILED),
            )
            return
        except CommandValidationError as exc:
            logger.warning("Rejected async command: %s", exc)
            await self.ctx.send_frame(
                Status.ERROR.value,
                encode_status_reason(protocol.STATUS_REASON_COMMAND_VALIDATION_FAILED),
            )
            await self._publish_run_async_error(protocol.STATUS_REASON_COMMAND_VALIDATION_FAILED)
            return

        match pid:
            case protocol.INVALID_ID_SENTINEL:
                await self.ctx.send_frame(
                    Status.ERROR.value,
                    encode_status_reason(protocol.STATUS_REASON_PROCESS_RUN_ASYNC_FAILED),
                )
                await self._publish_run_async_error(protocol.STATUS_REASON_PROCESS_RUN_ASYNC_FAILED)
                return
            case _:
                # [SIL-2] Use structured packet
                response = ProcessRunAsyncResponsePacket(pid=pid).encode()
                await self.ctx.send_frame(
                    Command.CMD_PROCESS_RUN_ASYNC_RESP.value,
                    response,
                )
                topic = topic_path(
                    self.state.mqtt_topic_prefix,
                    Topic.SHELL,
                    ShellAction.RUN_ASYNC,
                    protocol.MQTT_SUFFIX_RESPONSE,
                )
                await self.ctx.publish(
                    topic=topic,
                    payload=str(pid).encode(),
                )

    async def _publish_run_async_error(self, reason: str) -> None:
        topic = topic_path(
            self.state.mqtt_topic_prefix,
            Topic.SHELL,
            ShellAction.RUN_ASYNC,
            protocol.MQTT_SUFFIX_ERROR,
        )
        error_payload = msgspec.json.encode(
            {
                "status": "error",
                "reason": reason,
            }
        )
        await self.ctx.publish(topic=topic, payload=error_payload)

    async def handle_poll(self, payload: bytes) -> bool:
        try:
            packet = ProcessPollPacket.decode(payload)
            pid = packet.pid
        except (ConstructError, ValueError):
            logger.warning("Invalid PROCESS_POLL payload: %s", payload.hex())
            # Send empty/error response using structured packet
            error_resp = ProcessPollResponsePacket(
                status=Status.MALFORMED.value,
                exit_code=PROCESS_DEFAULT_EXIT_CODE,
                stdout=b"",
                stderr=b"",
            ).encode()
            await self.ctx.send_frame(
                Command.CMD_PROCESS_POLL_RESP.value,
                error_resp,
            )
            return False

        batch = await self.collect_output(pid)

        # [SIL-2] Use structured packet
        response_payload = ProcessPollResponsePacket(
            status=batch.status_byte,
            exit_code=batch.exit_code,
            stdout=batch.stdout_chunk,
            stderr=batch.stderr_chunk,
        ).encode()

        await self.ctx.send_frame(Command.CMD_PROCESS_POLL_RESP.value, response_payload)

        await self.publish_poll_result(pid, batch)
        if batch.finished:
            logger.debug("Sent final output for finished process PID %d", pid)
        return True

    async def handle_kill(self, payload: bytes, *, send_ack: bool = True) -> bool:
        try:
            packet = ProcessKillPacket.decode(payload)
            pid = packet.pid
        except (ConstructError, ValueError):
            logger.warning(
                "Invalid PROCESS_KILL payload. Expected 2 bytes, got %d: %s",
                len(payload),
                payload.hex(),
            )
            await self.ctx.send_frame(
                Status.MALFORMED.value,
                encode_status_reason(protocol.STATUS_REASON_PROCESS_KILL_MALFORMED),
            )
            return False

        async with self.state.process_lock:
            slot = self.state.running_processes.get(pid)
            proc = slot.handle if slot is not None else None

        if proc is None:
            logger.warning("Attempted to kill non-existent PID: %d", pid)
            await self.ctx.send_frame(
                Status.ERROR.value,
                encode_status_reason(protocol.STATUS_REASON_PROCESS_NOT_FOUND),
            )
            return send_ack

        try:
            await self._terminate_process_tree(proc)
            try:
                async with asyncio.timeout(PROCESS_KILL_WAIT_TIMEOUT):
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
        finally:
            released_slot = False
            async with self.state.process_lock:
                slot = self.state.running_processes.get(pid)
                if slot is not None:
                    # [FSM] Trigger cleanup via FSM transition
                    try:
                        slot.trigger("force_kill")
                    except Exception as e:
                        logger.error("FSM transition failed in handle_kill: %s", e)

                    slot.handle = None
                    slot.exit_code = proc.returncode if proc.returncode is not None else PROCESS_DEFAULT_EXIT_CODE

                    # If buffers are drained and FSM is zombie, remove it
                    if slot.is_drained() and slot.fsm_state == PROCESS_STATE_ZOMBIE:
                        self.state.running_processes.pop(pid, None)
                        released_slot = True
                    # If not drained, leave it as ZOMBIE for poll to clean up
            if released_slot:
                self._release_process_slot()

        await self.ctx.send_frame(Status.OK.value, b"")
        return send_ack

    async def run_sync(self, command: str, tokens: list[str]) -> tuple[int, bytes, bytes, int | None]:
        # Validation is done by caller via _prepare_command
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
        except BaseExceptionGroup as exc_group:
            matched, remainder = exc_group.split((OSError, RuntimeError, ValueError))
            if matched is None:
                raise
            logger.error(
                "IO Error interacting with process '%s': %s",
                command,
                matched,
            )
            await self._terminate_process_tree(proc)
            try:
                await proc.wait()
            except (OSError, ValueError):
                pass
            if remainder is not None:
                raise remainder
            return Status.ERROR.value, b"", b"System IO error", None
        except (OSError, RuntimeError, ValueError) as e:
            logger.error(
                "IO Error interacting with process '%s': %s",
                command,
                e,
            )
            await self._terminate_process_tree(proc)
            try:
                await proc.wait()
            except (OSError, ValueError):
                pass
            return Status.ERROR.value, b"", b"System IO error", None

        try:
            timed_out = bool(wait_task.result())
        except (OSError, ValueError, RuntimeError):
            logger.debug("Failed to retrieve sync process wait result", exc_info=True)
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

    async def _wait_for_sync_completion(self, proc: Process, pid_hint: int) -> bool:
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
                async with asyncio.timeout(PROCESS_SYNC_KILL_WAIT_TIMEOUT):
                    await proc.wait()
            except TimeoutError:
                logger.warning(
                    "Synchronous process PID %d did not exit after kill",
                    pid_hint,
                )
            return True

    async def start_async(self, command: str, tokens: list[str]) -> int:
        # Validation is done by caller via _prepare_command
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

            stack.pop_all()

        slot = ManagedProcess(pid=pid, command=command, handle=proc)
        # [FSM] Initialize state
        try:
            slot.trigger("start")
        except Exception as e:
            logger.error("FSM transition failed in start_async: %s", e)

        async with self.state.process_lock:
            self.state.running_processes[pid] = slot

        await self.ctx.schedule_background(
            self._monitor_async_process(pid, proc),
            name=f"process-monitor-{pid}",
        )
        logger.info("Started async process '%s' with PID %d", command, pid)
        return pid

    async def collect_output(self, pid: int) -> ProcessOutputBatch:
        async with self.state.process_lock:
            slot = self.state.running_processes.get(pid)

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

        stdout_truncated_limit = False
        stderr_truncated_limit = False

        # [FSM] Use io_lock for buffer access to support concurrency and testing hooks
        async with slot.io_lock:
            (
                stdout_payload,
                stderr_payload,
                payload_trunc_out,
                payload_trunc_err,
            ) = slot.pop_payload(_PROCESS_POLL_BUDGET)

        stdout_truncated_limit |= payload_trunc_out
        stderr_truncated_limit |= payload_trunc_err

        released_slot = False
        log_finished = False
        exit_value = PROCESS_DEFAULT_EXIT_CODE

        async with self.state.process_lock:
            # Re-check existence in case it was removed during io_lock (test scenario)
            current_slot = self.state.running_processes.get(pid)
            if current_slot is None or current_slot is not slot:
                return ProcessOutputBatch(
                    status_byte=Status.ERROR.value,
                    exit_code=PROCESS_DEFAULT_EXIT_CODE,
                    stdout_chunk=b"",
                    stderr_chunk=b"",
                    finished=False,
                    stdout_truncated=False,
                    stderr_truncated=False,
                )

            # [FSM] Determine finished status
            is_done = slot.fsm_state in (PROCESS_STATE_FINISHED, PROCESS_STATE_ZOMBIE)

            if is_done and slot.is_drained():
                if slot.fsm_state == PROCESS_STATE_FINISHED:
                    try:
                        slot.trigger("finalize")
                    except Exception as e:
                        logger.error("FSM transition failed in collect_output: %s", e)

                self.state.running_processes.pop(pid, None)
                released_slot = True
                log_finished = True

            exit_value = slot.exit_code if slot.exit_code is not None else PROCESS_DEFAULT_EXIT_CODE

        if released_slot:
            self._release_process_slot()

        if log_finished:
            logger.info(
                "Async process %d finished with exit code %d (Final Poll)",
                pid,
                exit_value,
            )

        return ProcessOutputBatch(
            status_byte=Status.OK.value,
            exit_code=exit_value & UINT8_MASK,
            stdout_chunk=stdout_payload,
            stderr_chunk=stderr_payload,
            finished=released_slot,
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
            if not chunk:
                break
            buffer.extend(chunk)

    async def _read_process_pipes(self, pid: int, proc: Process) -> tuple[bytes, bytes]:
        async with asyncio.TaskGroup() as tg:
            stdout_task = tg.create_task(self._read_stream_chunk(pid, proc.stdout))
            stderr_task = tg.create_task(self._read_stream_chunk(pid, proc.stderr))
        return stdout_task.result(), stderr_task.result()

    async def _drain_process_pipes(self, pid: int, proc: Process) -> tuple[bytes, bytes]:
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
        payload = msgspec.json.encode(
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
        )
        await self.ctx.publish(
            topic=topic,
            payload=payload,
            content_type="application/json",
            expiry=MQTT_EXPIRY_SHELL,
            properties=(("bridge-process-pid", str(pid)),),
        )

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

    async def _terminate_process_tree(self, proc: Process) -> None:
        if proc.returncode is not None:
            return
        pid_value = getattr(proc, "pid", None)
        if pid_value is None:
            try:
                proc.kill()
            except ProcessLookupError:
                pass
            return
        pid = int(pid_value)
        await asyncio.to_thread(self._kill_process_tree_sync, pid)
        try:
            proc.kill()
        except ProcessLookupError:
            pass

    async def _monitor_async_process(
        self,
        pid: int,
        proc: Process,
    ) -> None:
        try:
            await proc.wait()
        except asyncio.CancelledError:
            raise
        await self._finalize_async_process(pid, proc)

    async def _finalize_async_process(
        self,
        pid: int,
        proc: Process,
    ) -> None:
        async with self.state.process_lock:
            slot = self.state.running_processes.get(pid)
        if slot is None:
            self._release_process_slot()
            return

        # [FSM] Trigger SIGCHLD
        try:
            slot.trigger("sigchld")
        except Exception as e:
            logger.error("FSM transition failed in finalize (sigchld): %s", e)

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
                # Slot was replaced or removed? Should be rare/impossible if pid unique
                return

            # [FSM] Complete IO
            try:
                current_slot.trigger("io_complete")
            except Exception as e:
                logger.error("FSM transition failed in finalize (io_complete): %s", e)

            if stdout_tail or stderr_tail:
                current_slot.append_output(
                    stdout_tail,
                    stderr_tail,
                    limit=self.state.process_output_limit,
                )

            current_slot.exit_code = exit_value
            current_slot.handle = None

            # Check if we can cleanup immediately
            if current_slot.is_drained() and current_slot.fsm_state == PROCESS_STATE_FINISHED:
                 try:
                     current_slot.trigger("finalize")
                 except Exception:
                     pass
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
        targets = children + [process]

        for proc in targets:
            try:
                terminate = getattr(proc, "terminate", None)
                if callable(terminate):
                    terminate()
                else:
                    proc.kill()
            except (AttributeError, psutil.Error):
                continue

        try:
            _, alive = psutil.wait_procs(
                targets,
                timeout=max(0.1, PROCESS_KILL_WAIT_TIMEOUT),
            )
        except (AttributeError, TypeError, ValueError, psutil.Error):
            alive = targets
        if not alive:
            return

        for proc in alive:
            try:
                proc.kill()
            except (AttributeError, psutil.Error):
                continue

        try:
            psutil.wait_procs(
                alive,
                timeout=max(0.1, PROCESS_SYNC_KILL_WAIT_TIMEOUT),
            )
        except (AttributeError, TypeError, ValueError, psutil.Error):
            return

    def _build_sync_response(self, status: int, stdout_bytes: bytes, stderr_bytes: bytes) -> bytes:
        max_payload = MAX_PAYLOAD_SIZE - 5
        stdout_trim = stdout_bytes[:max_payload]
        remaining = max_payload - len(stdout_trim)
        stderr_trim = stderr_bytes[:remaining]
        return b"".join(
            [
                bytes([status & UINT8_MASK]),
                UINT16_STRUCT.build(len(stdout_trim)),
                stdout_trim,
                UINT16_STRUCT.build(len(stderr_trim)),
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
        except (TimeoutError, asyncio.TimeoutError):
            return False

    def _release_process_slot(self) -> None:
        guard = self._process_slots
        if guard is None:
            return
        try:
            guard.release()
        except ValueError:
            logger.debug("Process slot release requested with no available permits", exc_info=True)


__all__ = ["ProcessComponent", "ProcessOutputBatch"]
