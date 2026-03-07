"""Process management component for McuBridge."""

from __future__ import annotations

import asyncio
import contextlib
import logging
from collections.abc import Awaitable, Callable
from typing import TYPE_CHECKING, Any

import msgspec
import sh  # type: ignore[reportMissingTypeStubs]

from ..protocol import protocol, structures
from ..protocol.protocol import Status
from ..protocol.topics import Topic, topic_path
from ..protocol.structures import (
    ProcessOutputBatch,
    QueuedPublish,
)
from ..state.context import (
    PROCESS_STATE_FINISHED,
    ManagedProcess,
    RuntimeState,
)

if TYPE_CHECKING:
    from .runtime import BridgeService

logger = logging.getLogger("mcubridge.services.process")

PublishEnqueue = Callable[[QueuedPublish], Awaitable[None]]


class ProcessComponent:
    """Component for managing subprocess execution and output capture.

    [SIL-2] Deterministic Execution Model:
    - Limited concurrent processes.
    - Bounded output buffers per process.
    - Periodic polling for status and output.
    """

    def __init__(
        self,
        config: Any,
        state: RuntimeState,
        service: BridgeService,
    ) -> None:
        self.config = config
        self.state = state
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
                extra=msgspec.json.encode(batch),
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
        """Start a command asynchronously and return its PID using sh."""
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

            loop = asyncio.get_running_loop()

            def _out_cb(chunk: bytes | str) -> None:
                if not chunk:
                    return
                b_chunk = chunk if isinstance(chunk, bytes) else chunk.encode("utf-8", "replace")
                loop.call_soon_threadsafe(self._append_chunk_sync, pid, b_chunk, False)

            def _err_cb(chunk: bytes | str) -> None:
                if not chunk:
                    return
                b_chunk = chunk if isinstance(chunk, bytes) else chunk.encode("utf-8", "replace")
                loop.call_soon_threadsafe(self._append_chunk_sync, pid, b_chunk, True)

            def _done_cb(cmd: Any, success: bool, exit_code: int) -> None:
                loop.call_soon_threadsafe(self._finalize_sync, pid, exit_code)

            handle = sh.Command("/bin/sh")(
                "-c", command,
                _bg=True,
                _bg_exc=False,
                _out=_out_cb,
                _err=_err_cb,
                _done=_done_cb,
                _out_bufsize=1024,
                _err_bufsize=1024
            )

            proc.handle = handle
            proc.trigger("start")
            return pid
        except Exception as exc:
            logger.error("Failed to start process: %s", exc)
            self._process_slots.release()
            return 0

    async def _allocate_pid(self) -> int:
        """Atomically allocate a unique PID for a new process."""
        async with self.state.process_lock:
            pid = self.state.next_pid
            self.state.next_pid = (pid % 65535) + 1
            return pid

    def _append_chunk_sync(self, pid: int, chunk: bytes, is_stderr: bool) -> None:
        asyncio.create_task(self._append_chunk_async(pid, chunk, is_stderr))

    async def _append_chunk_async(self, pid: int, chunk: bytes, is_stderr: bool) -> None:
        async with self.state.process_lock:
            proc = self.state.running_processes.get(pid)
        if not proc:
            return
        async with proc.io_lock:
            if is_stderr:
                proc.stderr_buffer.extend(chunk)
            else:
                proc.stdout_buffer.extend(chunk)

    def _finalize_sync(self, pid: int, exit_code: int) -> None:
        asyncio.create_task(self._finalize_callback_async(pid, exit_code))

    async def _finalize_callback_async(self, pid: int, exit_code: int) -> None:
        async with self.state.process_lock:
            proc = self.state.running_processes.get(pid)
        if not proc:
            return
        async with proc.io_lock:
            proc.exit_code = exit_code
            proc.trigger("sigchld")
            proc.trigger("io_complete")
        async with self.state.process_lock:
            if proc.is_drained():
                self._finalize_process_internal(pid)

    async def poll_process(self, pid: int) -> ProcessOutputBatch:
        """Fetch pending output and status for a running process."""
        async with self.state.process_lock:
            proc = self.state.running_processes.get(pid)
            if not proc:
                return ProcessOutputBatch(Status.ERROR.value, 1, b"", b"", True, False, False)

            async with proc.io_lock:
                stdout, stderr, t_out, t_err = proc.pop_payload(protocol.MAX_PAYLOAD_SIZE - 32)
                is_finished = proc.fsm_state == PROCESS_STATE_FINISHED

                batch = ProcessOutputBatch(
                    Status.OK.value,
                    proc.exit_code or 0,
                    stdout,
                    stderr,
                    is_finished,
                    t_out,
                    t_err,
                )
                if is_finished and proc.is_drained():
                    self._finalize_process_internal(pid)
                return batch

    async def stop_process(self, pid: int) -> bool:
        """Terminate a running process."""
        async with self.state.process_lock:
            proc = self.state.running_processes.get(pid)
            if proc:
                if proc.handle:
                    with contextlib.suppress(Exception):
                        proc.handle.process.terminate()
                return True
            return False

    async def _start_async_subprocess(self, command: str) -> int:
        return await self.run_async(command)

    async def publish_poll_result(self, pid: int, batch: ProcessOutputBatch) -> None:
        """Publish process output batch to MQTT."""
        response_topic = topic_path(
            self.state.mqtt_topic_prefix,
            Topic.SHELL,
            protocol.ShellAction.POLL,
            str(pid),
            protocol.MQTT_SUFFIX_RESPONSE,
        )
        await self.service.enqueue_mqtt(
            QueuedPublish(
                topic_name=response_topic,
                payload=msgspec.json.encode(batch),
                content_type="application/json",
            )
        )

    async def _finalize_process(self, pid: int) -> None:
        async with self.state.process_lock:
            self._finalize_process_internal(pid)

    def _finalize_process_internal(self, pid: int) -> None:
        proc = self.state.running_processes.pop(pid, None)
        if proc:
            with contextlib.suppress(Exception):
                proc.trigger("finalize")
            self._process_slots.release()


__all__ = ["ProcessComponent", "ProcessOutputBatch"]

