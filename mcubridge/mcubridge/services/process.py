"""Process management component for McuBridge."""

from __future__ import annotations

import asyncio
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
        # [COMPAT] Legacy alias for coverage tests
        self.ctx = service

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

    async def handle_run(self, payload: bytes) -> None:
        """Handle synchronous process execution (deprecated)."""
        await self.service._acknowledge_mcu_frame(  # type: ignore[reportPrivateUsage]
            protocol.Command.CMD_PROCESS_RUN.value,
            status=Status.NOT_IMPLEMENTED,
        )

    async def handle_run_async(self, payload: bytes) -> None:
        """Handle async process execution request from MCU."""
        try:
            # 1. Decode attempt
            try:
                packet = structures.ProcessRunAsyncPacket.decode(payload)
                command = packet.command
            except Exception:
                # Fallback for simple raw string payloads from legacy tests
                command = payload.decode("utf-8", errors="ignore").strip()

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
            try:
                packet = structures.ProcessPollPacket.decode(payload)
                pid = packet.pid
            except Exception:
                pid = structures.UINT16_STRUCT.parse(payload)

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
            try:
                packet = structures.ProcessKillPacket.decode(payload)
                pid = packet.pid
            except Exception:
                pid = structures.UINT16_STRUCT.parse(payload)

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

    # --- Compatibility Methods for Legacy Tests ---

    async def run_sync(self, command: str, tokens: list[str] | None = None) -> tuple[int, bytes, bytes, int | None]:
        """Mock sync execution using async primitives."""
        pid = await self.run_async(command)
        if pid == 0:
            return Status.ERROR.value, b"", b"limit reached", 1

        while True:
            batch = await self.poll_process(pid)
            async with self.state.process_lock:
                if pid not in self.state.running_processes:
                    return Status.OK.value, batch.stdout_chunk, batch.stderr_chunk, batch.exit_code
            await asyncio.sleep(0.01)

    async def collect_output(self, pid: int) -> ProcessOutputBatch:
        return await self.poll_process(pid)

    async def start_async(self, command: str, tokens: Any = None) -> int:
        return await self.run_async(command)

    def _try_acquire_process_slot(self) -> bool:
        return not self._process_slots.locked()

    def _release_process_slot(self) -> None:
        try:
            self._process_slots.release()
        except ValueError:
            pass

    async def _terminate_process_tree(self, proc: Any) -> None:
        if hasattr(proc, "terminate"):
            try:
                proc.terminate()
            except Exception:
                pass

    async def _finalize_async_process(self, pid: int, proc: Any = None) -> None:
        await self._finalize_process(pid)

    async def _allocate_pid(self) -> int:
        async with self.state.process_lock:
            pid = self.state.next_pid
            self.state.next_pid = (pid + 1) % 65535 or 1
            return pid

    @staticmethod
    def _kill_process_tree_sync(pid: int) -> None:
        import psutil
        try:
            p = psutil.Process(pid)
            for child in p.children(recursive=True):
                try:
                    child.kill()
                except (psutil.Error, Exception):
                    pass
            p.kill()
        except (psutil.NoSuchProcess, Exception):
            pass

    async def _execute_sync_command(self, command: str, tokens: list[str]) -> None:
        status, stdout, stderr, _ = await self.run_sync(command, tokens)
        await self.service._acknowledge_mcu_frame(  # type: ignore[reportPrivateUsage]
            protocol.Command.CMD_PROCESS_RUN.value,
            status=Status(status),
            extra=stdout + stderr
        )

    def _limit_sync_payload(self, stdout: bytes, stderr: bytes) -> tuple[bytes, bytes]:
        return stdout[:1024], stderr[:1024]

    # --- Core Logic ---

    async def run_async(self, command: str) -> int:
        """Start a command asynchronously and return its PID using sh."""
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
        except Exception as e:
            logger.error("Failed to start process: %s", e)
            self._process_slots.release()
            return 0

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
                    try:
                        proc.handle.process.terminate()
                    except Exception:
                        pass
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
            try:
                proc.trigger("finalize")
            except Exception:
                pass
            self._process_slots.release()


__all__ = ["ProcessComponent", "ProcessOutputBatch"]

