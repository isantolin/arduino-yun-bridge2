from __future__ import annotations

import asyncio
import contextlib
import logging
from collections.abc import Awaitable, Callable
from typing import TYPE_CHECKING, Any

import msgspec
import psutil
from aiomqtt.message import Message

from ..protocol import protocol, structures
from ..protocol.protocol import ShellAction, Status
from ..protocol.structures import (
    ProcessOutputBatch,
    QueuedPublish,
)
from ..protocol.topics import Topic, topic_path
from ..state.context import (
    PROCESS_STATE_FINISHED,
    ManagedProcess,
    RuntimeState,
)
from .base import BaseComponent
from .payloads import (
    PayloadValidationError,
    ShellCommandPayload,
    ShellPidPayload,
)

if TYPE_CHECKING:
    from .runtime import BridgeService

logger = logging.getLogger("mcubridge.services.process")

PublishEnqueue = Callable[[QueuedPublish], Awaitable[None]]


class ProcessComponent(BaseComponent):
    """Component for managing subprocess execution and output capture.

    [SIL-2] Deterministic Execution Model:
    - Limited concurrent processes.
    - Bounded output buffers per process.
    - Periodic polling for status and output.
    - Unified handling for MCU and MQTT shell requests.
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

    # --- MQTT Handlers ---

    async def handle_mqtt(
        self,
        segments: list[str],
        payload: bytes,
        inbound: Message | None = None,
    ) -> None:
        """Handle shell-related MQTT topics."""
        if not segments:
            return

        action = segments[0]

        match action:
            case ShellAction.RUN_ASYNC:
                payload_model = self._parse_shell_command(payload, action)
                if payload_model is None:
                    return
                await self._handle_mqtt_run_async(payload_model, inbound)

            case ShellAction.POLL if len(segments) == 2:
                pid_model = self._parse_shell_pid(segments[1], action)
                if pid_model is None:
                    return
                await self._handle_mqtt_poll(pid_model)

            case ShellAction.KILL if len(segments) == 2:
                pid_model = self._parse_shell_pid(segments[1], action)
                if pid_model is None:
                    return
                await self._handle_mqtt_kill(pid_model)

            case _:
                logger.debug(
                    "Ignoring shell topic action: %s",
                    "/".join(segments),
                )

    async def _handle_mqtt_run_async(
        self,
        payload: ShellCommandPayload,
        inbound: Message | None,
    ) -> None:
        command = payload.command
        logger.info("MQTT async shell command: '%s'", command)
        try:
            pid = await self.run_async(command)
        except (OSError, ValueError, RuntimeError) as exc:
            logger.error("Error starting async command: %s", exc)
            response_topic = topic_path(
                self.state.mqtt_topic_prefix,
                Topic.SHELL,
                ShellAction.RUN_ASYNC,
                "error",
            )
            await self.service.publish(
                topic=response_topic,
                payload=b"error:internal",
                reply_to=inbound,
            )
            return

        response_topic = topic_path(
            self.state.mqtt_topic_prefix,
            Topic.SHELL,
            ShellAction.RUN_ASYNC,
            protocol.MQTT_SUFFIX_RESPONSE,
        )

        if pid == 0:
            await self.service.publish(
                topic=response_topic,
                payload=b"error:not_allowed_or_limit_reached",
                reply_to=inbound,
            )
            return

        await self.service.publish(
            topic=response_topic,
            payload=str(pid).encode("utf-8"),
            reply_to=inbound,
        )

    async def _handle_mqtt_poll(self, pid_model: ShellPidPayload) -> None:
        pid = pid_model.pid
        batch = await self.poll_process(pid)
        await self.publish_poll_result(pid, batch)

    async def _handle_mqtt_kill(self, pid_model: ShellPidPayload) -> None:
        await self.stop_process(pid_model.pid)

    def _parse_shell_command(
        self,
        payload: bytes,
        action: str,
    ) -> ShellCommandPayload | None:
        try:
            return ShellCommandPayload.from_mqtt(payload)
        except PayloadValidationError as exc:
            logger.warning(
                "Invalid shell/%s payload: %s",
                action,
                exc.message,
            )
            return None

    def _parse_shell_pid(
        self,
        segment: str,
        action: str,
    ) -> ShellPidPayload | None:
        try:
            return ShellPidPayload.from_topic_segment(segment)
        except PayloadValidationError as exc:
            logger.warning(
                "Invalid shell/%s PID: %s",
                action,
                exc.message,
            )
            return None

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
                await self.service._acknowledge_mcu_frame(  # type: ignore[reportPrivateUsage]
                    protocol.Command.CMD_PROCESS_RUN_ASYNC.value,
                    status=Status.OK,
                )
                resp = structures.ProcessRunAsyncResponsePacket(pid=pid).encode()
                await self.service.send_frame(
                    protocol.Command.CMD_PROCESS_RUN_ASYNC_RESP.value, resp,
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
            )
            resp = structures.ProcessPollResponsePacket(
                status=batch.status_byte,
                exit_code=batch.exit_code,
                stdout_data=batch.stdout_chunk,
                stderr_data=batch.stderr_chunk,
            ).encode()
            await self.service.send_frame(
                protocol.Command.CMD_PROCESS_POLL_RESP.value, resp,
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
        """Start a command asynchronously using asyncio.subprocess."""
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

            try:
                # [OPT] Use asyncio's native subprocess for zero-thread execution
                process = await asyncio.create_subprocess_shell(
                    command,
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.PIPE,
                )
            except OSError as e:
                logger.error("Failed to spawn process: %s", e)
                self._process_slots.release()
                async with self.state.process_lock:
                    self.state.running_processes.pop(pid, None)
                return 0

            proc.handle = process
            proc.trigger("start")

            # Spawn reader tasks
            asyncio.create_task(self._read_stream(pid, process.stdout, False))
            asyncio.create_task(self._read_stream(pid, process.stderr, True))
            asyncio.create_task(self._wait_process(pid, process))

            return pid
        except (OSError, ValueError, RuntimeError) as exc:
            logger.error("Failed to start process: %s", exc)
            self._process_slots.release()
            return 0

    async def _allocate_pid(self) -> int:
        """Atomically allocate a unique PID for a new process."""
        async with self.state.process_lock:
            pid = self.state.next_pid
            self.state.next_pid = (pid % 65535) + 1
            return pid

    async def _read_stream(self, pid: int, stream: asyncio.StreamReader | None, is_stderr: bool) -> None:
        """Read output from a subprocess stream non-blockingly."""
        if not stream:
            return
        try:
            while True:
                chunk = await stream.read(1024)
                if not chunk:
                    break
                await self._append_chunk_async(pid, chunk, is_stderr)
        except (OSError, asyncio.CancelledError):
            pass

    async def _wait_process(self, pid: int, process: asyncio.subprocess.Process) -> None:
        """Wait for subprocess to finish and capture exit code."""
        try:
            exit_code = await process.wait()
            await self._finalize_callback_async(pid, exit_code)
        except asyncio.CancelledError:
            with contextlib.suppress(ProcessLookupError):
                process.terminate()
            await self._finalize_callback_async(pid, -1)

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
        """Terminate a running process and its children recursively."""
        async with self.state.process_lock:
            proc_entry = self.state.running_processes.get(pid)
            if not proc_entry or not proc_entry.handle:
                return False

            try:
                # [SIL-2] Use psutil to kill the entire process tree
                parent = psutil.Process(proc_entry.handle.pid)
                children = parent.children(recursive=True)
                for child in children:
                    with contextlib.suppress(psutil.NoSuchProcess, ProcessLookupError):
                        child.terminate()

                with contextlib.suppress(psutil.NoSuchProcess, ProcessLookupError):
                    parent.terminate()

                # Brief wait for graceful termination before force-killing
                if children or parent:
                    try:
                        _, alive = psutil.wait_procs(children + [parent], timeout=0.2)
                        for proc in alive:
                            with contextlib.suppress(psutil.NoSuchProcess, ProcessLookupError):
                                proc.kill()
                    except (psutil.NoSuchProcess, ProcessLookupError):
                        pass

                return True
            except (psutil.NoSuchProcess, ProcessLookupError):
                return True  # Process already gone is a success for us
            except (psutil.AccessDenied, OSError, RuntimeError, ValueError) as e:
                logger.error("Error stopping process %d: %s", pid, e)
                return False


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
                payload=msgspec.msgpack.encode(batch),
                content_type="application/msgpack",
            )
        )

    async def _finalize_process(self, pid: int) -> None:
        async with self.state.process_lock:
            self._finalize_process_internal(pid)

    def _finalize_process_internal(self, pid: int) -> None:
        proc = self.state.running_processes.pop(pid, None)
        if proc:
            with contextlib.suppress(AttributeError, ValueError):
                proc.trigger("finalize")
            self._process_slots.release()


__all__ = ["ProcessComponent", "ProcessOutputBatch"]
