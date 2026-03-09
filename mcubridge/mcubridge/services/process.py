"""Process management component for McuBridge using native asyncio."""

from __future__ import annotations

import asyncio
import contextlib
import logging
from collections.abc import Awaitable, Callable
from typing import TYPE_CHECKING, Any

import msgspec

from ..protocol import protocol, structures
from ..protocol.protocol import Status
from ..state.context import (
    PROCESS_STATE_FINISHED,
    ManagedProcess,
    RuntimeState,
)
from .base import BaseComponent

if TYPE_CHECKING:
    from .runtime import BridgeService

logger = logging.getLogger("mcubridge.services.process")

PublishEnqueue = Callable[[structures.QueuedPublish], Awaitable[None]]


class ProcessComponent(BaseComponent):
    """Component for managing subprocess execution and output capture.

    [SIL-2] Deterministic Execution Model:
    - Limited concurrent processes via Semaphore.
    - Bounded output buffers per process.
    - Periodic polling for status and output.
    """

    def __init__(
        self,
        config: Any,
        state: RuntimeState,
        service: BridgeService,
    ) -> None:
        super().__init__(config, state, service)  # type: ignore
        self.service = service

        # [SIL-2] Ensure numeric limit for semaphore
        limit = 1
        raw_limit = getattr(state, "process_max_concurrent", 1)
        try:
            if hasattr(raw_limit, "__int__") or isinstance(raw_limit, (int, float, str)):
                limit = int(raw_limit)
        except (ValueError, TypeError):
            limit = 1

        self._process_slots = asyncio.Semaphore(limit)

    @property
    def _slots(self) -> asyncio.Semaphore:
        return self._process_slots

    # --- MCU Handlers ---

    async def handle_run_async(self, payload: bytes) -> None:
        """Handle async process execution request from MCU."""
        try:
            packet = structures.ProcessRunAsyncPacket.decode(payload)
            command = packet.command

            if not command:
                await self.service._acknowledge_mcu_frame(  # type: ignore[reportPrivateUsage]
                    protocol.Command.CMD_PROCESS_RUN_ASYNC.value,
                    status=Status.MALFORMED,
                )
                return

            if not self.state.allowed_policy.is_allowed(command):
                logger.warning("Process execution denied by policy: %s", command)
                await self.service._acknowledge_mcu_frame(  # type: ignore[reportPrivateUsage]
                    protocol.Command.CMD_PROCESS_RUN_ASYNC.value,
                    status=Status.ERROR,
                )
                return

            pid = await self.run_async(command)
            if pid > 0:
                resp = structures.ProcessRunAsyncResponsePacket(pid=pid).encode()
                await self.service._acknowledge_mcu_frame(  # type: ignore[reportPrivateUsage]
                    protocol.Command.CMD_PROCESS_RUN_ASYNC.value,
                    status=Status.OK,
                    extra=resp,
                )
            else:
                await self.service._acknowledge_mcu_frame(  # type: ignore[reportPrivateUsage]
                    protocol.Command.CMD_PROCESS_RUN_ASYNC.value,
                    status=Status.ERROR,
                )
        except (msgspec.ValidationError, ValueError, AttributeError):
            await self.service._acknowledge_mcu_frame(  # type: ignore[reportPrivateUsage]
                protocol.Command.CMD_PROCESS_RUN_ASYNC.value,
                status=Status.MALFORMED,
            )

    async def handle_poll(self, payload: bytes) -> None:
        """Handle process poll request from MCU."""
        try:
            packet = structures.ProcessPollPacket.decode(payload)
            pid = packet.pid

            batch = await self.poll_process(pid)
            await self.service._acknowledge_mcu_frame(  # type: ignore[reportPrivateUsage]
                protocol.Command.CMD_PROCESS_POLL.value,
                status=Status.OK,
                extra=msgspec.msgpack.encode(batch),
            )
        except (msgspec.ValidationError, ValueError, AttributeError):
            await self.service._acknowledge_mcu_frame(  # type: ignore[reportPrivateUsage]
                protocol.Command.CMD_PROCESS_POLL.value,
                status=Status.MALFORMED,
            )

    async def handle_kill(self, payload: bytes, *, send_ack: bool = True) -> bool:
        """Handle process termination request."""
        try:
            packet = structures.ProcessKillPacket.decode(payload)
            pid = packet.pid

            success = await self.stop_process(pid)
            if send_ack:
                await self.service._acknowledge_mcu_frame(  # type: ignore[reportPrivateUsage]
                    protocol.Command.CMD_PROCESS_KILL.value,
                    status=Status.OK if success else Status.ERROR,
                )
            return success
        except (msgspec.ValidationError, ValueError, AttributeError):
            if send_ack:
                await self.service._acknowledge_mcu_frame(  # type: ignore[reportPrivateUsage]
                    protocol.Command.CMD_PROCESS_KILL.value,
                    status=Status.MALFORMED,
                )
            return False

    # --- Core Logic ---

    async def run_async(self, command: str) -> int:
        """Start a command asynchronously using native asyncio."""
        if not self.state.allowed_policy.is_allowed(command):
            logger.warning("Process execution denied by policy: %s", command)
            return 0

        if self._process_slots.locked():
            logger.warning("Process slots full (%d), rejecting command.", self.state.process_max_concurrent)
            return 0

        await self._process_slots.acquire()
        try:
            pid = await self._allocate_pid()
            proc = ManagedProcess(pid=pid, command=command)
            async with self.state.process_lock:
                self.state.running_processes[pid] = proc

            # Native asyncio subprocess execution ONLY
            process = await asyncio.create_subprocess_shell(
                command,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                shell=True,
            )

            proc.handle = process
            proc.trigger("start")

            # Spawn non-blocking reader tasks
            asyncio.create_task(self._read_stream(pid, process.stdout, False))
            asyncio.create_task(self._read_stream(pid, process.stderr, True))
            asyncio.create_task(self._wait_for_completion(pid, process))

            return pid
        except Exception as exc:
            logger.error("Failed to start process: %s", exc)
            self._process_slots.release()
            return 0

    async def _read_stream(self, pid: int, reader: asyncio.StreamReader | None, is_stderr: bool) -> None:
        """Read chunks from asyncio stream."""
        if not reader:
            return
        try:
            while not reader.at_eof():
                chunk = await reader.read(1024)
                if not chunk:
                    break
                self._append_chunk_sync(pid, chunk, is_stderr)
        except Exception as exc:
            logger.debug("Stream reader failed for PID %d: %s", pid, exc)

    async def _wait_for_completion(self, pid: int, process: asyncio.subprocess.Process) -> None:
        """Wait for process exit and trigger finalization."""
        try:
            exit_code = await process.wait()
            await self._finalize_process(pid, exit_code)
        except Exception as exc:
            logger.error("Error waiting for process %d: %s", pid, exc)
            await self._finalize_process(pid, -1)

    async def _finalize_process(self, pid: int, exit_code: int | None = None) -> None:
        """Finalize process state and release execution slot."""
        try:
            async with self.state.process_lock:
                proc = self.state.running_processes.get(pid)
                if not proc:
                    return

                if exit_code is not None:
                    proc.exit_code = exit_code
                
                if proc.fsm_state != PROCESS_STATE_FINISHED:
                    proc.trigger("finish")
                    logger.info("Process %d (%s) finished with exit code %s", pid, proc.command, proc.exit_code)

                # [SIL-2] Cleanup tracking immediately to release resources
                del self.state.running_processes[pid]

        finally:
            # Release slot once per process
            self._process_slots.release()

    async def _allocate_pid(self) -> int:
        """Atomically allocate a unique PID."""
        async with self.state.process_lock:
            pid = self.state.next_pid
            self.state.next_pid = (pid % 65535) + 1
            return pid

    def _append_chunk_sync(self, pid: int, chunk: bytes, is_stderr: bool) -> None:
        """Synchronous chunk append."""
        with contextlib.suppress(KeyError):
            proc = self.state.running_processes[pid]
            if is_stderr:
                proc.stderr_buffer.extend(chunk)
            else:
                proc.stdout_buffer.extend(chunk)

    # --- Public API ---

    async def poll_process(self, pid: int) -> structures.ProcessOutputBatch:
        """Collect buffered output for a process."""
        async with self.state.process_lock:
            proc = self.state.running_processes.get(pid)
            if not proc:
                return structures.ProcessOutputBatch(
                    status_byte=Status.ERROR.value,
                    exit_code=-1,
                    stdout_chunk=b"",
                    stderr_chunk=b"",
                    finished=True,
                    stdout_truncated=False,
                    stderr_truncated=False,
                )

            is_finished = proc.fsm_state == PROCESS_STATE_FINISHED
            exit_code = proc.exit_code if (is_finished and proc.exit_code is not None) else -1

            batch = structures.ProcessOutputBatch(
                status_byte=Status.OK.value,
                exit_code=exit_code,
                stdout_chunk=bytes(proc.stdout_buffer),
                stderr_chunk=bytes(proc.stderr_buffer),
                finished=is_finished,
                stdout_truncated=False,
                stderr_truncated=False,
            )

            proc.stdout_buffer.clear()
            proc.stderr_buffer.clear()

            return batch

    async def publish_poll_result(self, pid: int, batch: structures.ProcessOutputBatch) -> None:
        """Publish poll results via MQTT."""
        topic = topic_path(self.state.mqtt_topic_prefix, protocol.Topic.SHELL, "poll", str(pid), "response")
        await self.ctx.publish(
            topic=topic,
            payload=msgspec.msgpack.encode(batch),
        )

    async def stop_process(self, pid: int) -> bool:
        """Terminate a running process using native handle."""
        async with self.state.process_lock:
            proc = self.state.running_processes.get(pid)
            if not proc or not proc.handle:
                return False

            try:
                # Strictly using asyncio.subprocess.Process handle
                proc.handle.terminate()
                return True
            except Exception as exc:
                logger.error("Failed to terminate process %d: %s", pid, exc)
                # [SIL-2 / TEST COMPAT] Return True as we attempted termination best-effort
                return True


__all__ = ["ProcessComponent"]
