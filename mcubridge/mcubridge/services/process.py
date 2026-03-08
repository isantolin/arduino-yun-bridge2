"""Process management component for McuBridge."""

from __future__ import annotations

import asyncio
import contextlib
import logging
from collections.abc import Awaitable, Callable
from typing import TYPE_CHECKING, Any

import msgspec

from ..protocol import protocol, structures
from ..protocol.protocol import Status
from ..protocol.topics import topic_path
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

    # --- MCU Handlers (Required by Dispatcher) ---

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

            # 2. Policy check
            if not self.state.allowed_policy.is_allowed(command):
                logger.warning("Process execution denied by policy: %s", command)
                await self.service._acknowledge_mcu_frame(  # type: ignore[reportPrivateUsage]
                    protocol.Command.CMD_PROCESS_RUN_ASYNC.value,
                    status=Status.ERROR,
                )
                return

            # 3. Execution
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
        """Start a command asynchronously and return its PID using asyncio.create_subprocess_shell."""
        # [SECURITY] Enforce command policy at the lowest execution level
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

            # [SIL-2 / Modernization] Use native asyncio subprocess
            process = await asyncio.create_subprocess_shell(
                command,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                shell=True,
            )

            proc.handle = process
            proc.trigger("start")

            # Spawn reader tasks for non-blocking stream capture
            asyncio.create_task(self._read_stream(pid, process.stdout, False))
            asyncio.create_task(self._read_stream(pid, process.stderr, True))
            asyncio.create_task(self._wait_for_completion(pid, process))

            return pid
        except Exception as exc:
            logger.error("Failed to start process: %s", exc)
            self._process_slots.release()
            return 0

    async def _read_stream(self, pid: int, reader: asyncio.StreamReader | None, is_stderr: bool) -> None:
        """Efficiently read chunks from a stream and buffer them."""
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
        """Wait for process exit and finalize state."""
        try:
            exit_code = await process.wait()
            self._finalize_sync(pid, exit_code)
        finally:
            self._process_slots.release()

    async def _allocate_pid(self) -> int:
        """Atomically allocate a unique PID for a new process."""
        async with self.state.process_lock:
            pid = self.state.next_pid
            self.state.next_pid = (pid % 65535) + 1
            return pid

    def _append_chunk_sync(self, pid: int, chunk: bytes, is_stderr: bool) -> None:
        """Synchronous chunk append used by stream readers."""
        with contextlib.suppress(KeyError):
            proc = self.state.running_processes[pid]
            if is_stderr:
                proc.stderr_buffer.extend(chunk)
            else:
                proc.stdout_buffer.extend(chunk)

    def _finalize_sync(self, pid: int, exit_code: int) -> None:
        """Synchronous finalization used by process completion task."""
        with contextlib.suppress(KeyError):
            proc = self.state.running_processes[pid]
            proc.exit_code = exit_code
            proc.trigger("finish")
            logger.info("Process %d (%s) finished with exit code %d", pid, proc.command, exit_code)

    # --- Public API for state management ---

    async def poll_process(self, pid: int) -> structures.ProcessOutputBatch:
        """Collect and clear buffered output for a given process."""
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

            batch = structures.ProcessOutputBatch(
                status_byte=Status.OK.value,
                exit_code=proc.exit_code if (proc.fsm_state == PROCESS_STATE_FINISHED and proc.exit_code is not None) else -1,
                stdout_chunk=bytes(proc.stdout_buffer),
                stderr_chunk=bytes(proc.stderr_buffer),
                finished=(proc.fsm_state == PROCESS_STATE_FINISHED),
                stdout_truncated=False,
                stderr_truncated=False,
            )

            # Clear buffers after successful poll
            proc.stdout_buffer.clear()
            proc.stderr_buffer.clear()

            # Cleanup process if finished
            if proc.fsm_state == PROCESS_STATE_FINISHED:
                del self.state.running_processes[pid]

            return batch

    async def publish_poll_result(self, pid: int, batch: structures.ProcessOutputBatch) -> None:
        """Publish poll results via MQTT using the standardized topic structure."""
        topic = topic_path(self.state.mqtt_topic_prefix, protocol.Topic.SHELL, "poll", str(pid), "response")
        await self.ctx.publish(
            topic=topic,
            payload=msgspec.msgpack.encode(batch),
        )

    async def stop_process(self, pid: int) -> bool:
        """Terminate a running process."""
        async with self.state.process_lock:
            proc = self.state.running_processes.get(pid)
            if not proc or not proc.handle:
                return False

            try:
                # proc.handle is now a subprocess.Process
                proc.handle.terminate()
                return True
            except Exception as exc:
                logger.error("Failed to terminate process %d: %s", pid, exc)
                return False


__all__ = ["ProcessComponent"]
