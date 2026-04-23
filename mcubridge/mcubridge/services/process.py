from __future__ import annotations

import asyncio
import contextlib
import inspect
import structlog
from collections.abc import Awaitable, Callable
from typing import TYPE_CHECKING

import msgspec
import psutil
from aiomqtt.message import Message

from ..protocol import protocol, structures
from ..protocol.protocol import ShellAction, Status
from ..protocol.structures import (
    PayloadValidationError,
    ProcessOutputBatch,
    QueuedPublish,
    ShellCommandPayload,
    ShellPidPayload,
    TopicRoute,
)
from ..protocol.topics import Topic, topic_path
from ..state.context import (
    PROCESS_STATE_FINISHED,
    ManagedProcess,
)

if TYPE_CHECKING:
    from ..transport.mqtt import MqttTransport
    from ..state.context import RuntimeState
    from ..config.settings import RuntimeConfig
    from .serial_flow import SerialFlowController

logger = structlog.get_logger("mcubridge.services.process")
_msgpack_enc = msgspec.msgpack.Encoder()

PublishEnqueue = Callable[[QueuedPublish], Awaitable[None]]


class ProcessComponent:
    """Component for managing subprocess execution and output capture. [SIL-2]"""

    def __init__(
        self,
        config: RuntimeConfig,
        state: RuntimeState,
        serial_flow: SerialFlowController,
        mqtt_flow: MqttTransport,
    ) -> None:
        self.config = config
        self.state = state
        self.serial_flow = serial_flow
        self.mqtt_flow = mqtt_flow

        # [SIL-2] Ensure numeric limit for semaphore
        limit = int(state.process_max_concurrent)
        self._process_slots = asyncio.Semaphore(limit)

    @property
    def _slots(self) -> asyncio.Semaphore:
        return self._process_slots

    # --- MQTT Handlers ---

    async def handle_mqtt(
        self,
        route: TopicRoute,
        inbound: Message,
    ) -> bool:
        """Handle shell-related MQTT topics."""
        segments = list(route.segments)
        payload = msgspec.convert(inbound.payload, bytes)
        if not segments:
            return True

        action = segments[0]

        match action:
            case ShellAction.RUN_ASYNC:
                payload_model = self._parse_shell_command(payload, action)
                if payload_model is None:
                    return True
                await self._handle_mqtt_run_async(payload_model, inbound)

            case ShellAction.POLL if len(segments) == 2:
                pid_model = self._parse_shell_pid(segments[1], action)
                if pid_model is None:
                    return True
                await self._handle_mqtt_poll(pid_model, inbound)

            case ShellAction.KILL if len(segments) == 2:
                pid_model = self._parse_shell_pid(segments[1], action)
                if pid_model is None:
                    return True
                await self._handle_mqtt_kill(pid_model, inbound)

            case _:
                logger.debug(
                    "Ignoring shell topic action: %s",
                    "/".join(segments),
                )
        return True

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
            await self.mqtt_flow.publish(
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
            await self.mqtt_flow.publish(
                topic=response_topic,
                payload=b"error:not_allowed_or_limit_reached",
                reply_to=inbound,
            )
            return

        await self.mqtt_flow.publish(
            topic=response_topic,
            payload=str(pid).encode("utf-8"),
            reply_to=inbound,
        )

    async def _handle_mqtt_poll(self, pid_model: ShellPidPayload, inbound: Message | None = None) -> None:
        pid = pid_model.pid
        batch = await self.poll_process(pid)
        await self.publish_poll_result(pid, batch, inbound)

    async def _handle_mqtt_kill(self, pid_model: ShellPidPayload, inbound: Message | None = None) -> None:
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

    async def handle_run_async(self, seq_id: int, payload: bytes) -> None:
        """Handle async process execution request from MCU."""
        try:
            packet = msgspec.msgpack.decode(payload, type=structures.ProcessRunAsyncPacket)
            command = packet.command

            if not command:
                await self.serial_flow.acknowledge(
                    protocol.Command.CMD_PROCESS_RUN_ASYNC.value,
                    seq_id,
                    status=Status.MALFORMED,
                )
                return

            # 2. Policy check
            if not self.state.allowed_policy.is_allowed(command):
                logger.warning("Process execution denied by policy: %s", command)
                await self.serial_flow.acknowledge(
                    protocol.Command.CMD_PROCESS_RUN_ASYNC.value,
                    seq_id,
                    status=Status.ERROR,
                )
                return

            # 3. Execution
            pid = await self.run_async(command)
            if pid > 0:
                await self.serial_flow.acknowledge(
                    protocol.Command.CMD_PROCESS_RUN_ASYNC.value,
                    seq_id,
                    status=Status.OK,
                )
                resp = msgspec.msgpack.encode(structures.ProcessRunAsyncResponsePacket(pid=pid))
                await self.serial_flow.send(
                    protocol.Command.CMD_PROCESS_RUN_ASYNC_RESP.value,
                    resp,
                )
            else:
                await self.serial_flow.acknowledge(
                    protocol.Command.CMD_PROCESS_RUN_ASYNC.value,
                    seq_id,
                    status=Status.ERROR,
                )
        except (msgspec.ValidationError, msgspec.DecodeError, ValueError, AttributeError):
            await self.serial_flow.acknowledge(
                protocol.Command.CMD_PROCESS_RUN_ASYNC.value,
                seq_id,
                status=Status.MALFORMED,
            )

    async def handle_poll(self, seq_id: int, payload: bytes) -> None:
        """Handle process poll request from MCU."""
        try:
            packet = msgspec.msgpack.decode(payload, type=structures.ProcessPollPacket)
            pid = packet.pid

            batch = await self.poll_process(pid)
            await self.serial_flow.acknowledge(
                protocol.Command.CMD_PROCESS_POLL.value,
                seq_id,
                status=Status.OK,
            )
            resp = msgspec.msgpack.encode(
                structures.ProcessPollResponsePacket(
                    status=batch.status_byte,
                    exit_code=batch.exit_code,
                    stdout_data=batch.stdout_chunk,
                    stderr_data=batch.stderr_chunk,
                )
            )
            await self.serial_flow.send(
                protocol.Command.CMD_PROCESS_POLL_RESP.value,
                resp,
            )
        except (msgspec.ValidationError, ValueError, AttributeError):
            await self.serial_flow.acknowledge(
                protocol.Command.CMD_PROCESS_POLL.value,
                seq_id,
                status=Status.MALFORMED,
            )

    async def handle_kill(self, seq_id: int, payload: bytes, *, send_ack: bool = True) -> bool:
        """Handle process termination request."""
        try:
            packet = msgspec.msgpack.decode(payload, type=structures.ProcessKillPacket)
            pid = packet.pid

            success = await self.stop_process(pid)
            if send_ack:
                await self.serial_flow.acknowledge(
                    protocol.Command.CMD_PROCESS_KILL.value,
                    seq_id,
                    status=Status.OK if success else Status.ERROR,
                )
            return success
        except (msgspec.ValidationError, ValueError, AttributeError):
            if send_ack:
                await self.serial_flow.acknowledge(
                    protocol.Command.CMD_PROCESS_KILL.value,
                    seq_id,
                    status=Status.MALFORMED,
                )
            return False

    # --- Core Logic ---

    async def run_async(self, command: str) -> int:
        """Start a command asynchronously using native asyncio subprocess."""
        if not self.state.allowed_policy.is_allowed(command):
            logger.warning("Process execution denied by policy: %s", command)
            return 0

        # [SIL-2] Wait for an available process slot
        await self._process_slots.acquire()

        try:
            # [SIL-2] Use native asyncio subprocess for zero-thread execution
            # and deterministic lifecycle.
            import shlex

            args = shlex.split(command)
            process = await asyncio.create_subprocess_exec(
                *args,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                start_new_session=True,
            )

            # Use OS PID directly (guaranteed < 65536 on OpenWrt/standard Linux)
            pid = process.pid & 0xFFFF
            managed = ManagedProcess(pid=pid, command=command, handle=process)

            async with self.state.process_lock:
                self.state.running_processes[pid] = managed

            managed.trigger("start")

            # Spawn monitor task for completion/timeout only
            asyncio.create_task(self._monitor_process(pid))
            return pid

        except (OSError, ValueError, RuntimeError) as exc:
            logger.error("Failed to spawn process: %s", exc)
            self._process_slots.release()
            return 0

    async def _monitor_process(self, pid: int) -> None:
        """Monitor process lifecycle with safety timeouts to prevent slot deadlocks."""
        try:
            async with self.state.process_lock:
                proc = self.state.running_processes.get(pid)

            if proc and proc.handle:
                # [SIL-2] Non-blocking wait for process exit
                try:
                    proc.exit_code = await asyncio.wait_for(
                        proc.handle.wait(), timeout=float(self.state.process_timeout)
                    )
                except asyncio.TimeoutError:
                    logger.warning("Process %d monitor timed out; forcing finalization", pid)
                    with contextlib.suppress(OSError):
                        proc.handle.kill()
                    proc.exit_code = -1

                async with proc.io_lock:
                    proc.trigger("sigchld")
                    proc.trigger("io_complete")
        finally:
            self._finalize_process_internal(pid)

    async def poll_process(self, pid: int) -> ProcessOutputBatch:
        """Fetch pending output and status for a running process."""
        async with self.state.process_lock:
            proc = self.state.running_processes.get(pid)
            if not proc:
                return ProcessOutputBatch(Status.ERROR.value, 1, b"", b"", True, False, False)

            async with proc.io_lock:
                budget = protocol.MAX_PAYLOAD_SIZE - 32

                async def _read_stream(stream: asyncio.StreamReader | None) -> tuple[bytes, bool]:
                    if not stream or stream.at_eof():
                        return b"", False
                    try:
                        # Use direct read natively instead of intermediate deque
                        chunk = await asyncio.wait_for(stream.read(budget), timeout=0.01)
                        return chunk, not stream.at_eof()
                    except asyncio.TimeoutError:
                        return b"", True

                stdout_chunk = b""
                stderr_chunk = b""
                t_out = False
                t_err = False

                if proc.handle:
                    stdout_chunk, t_out = await _read_stream(proc.handle.stdout)
                    stderr_chunk, t_err = await _read_stream(proc.handle.stderr)
                is_finished = proc.fsm_state == PROCESS_STATE_FINISHED

                batch = ProcessOutputBatch(
                    Status.OK.value,
                    proc.exit_code or 0,
                    stdout_chunk,
                    stderr_chunk,
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
            handle = proc_entry.handle

        try:
            # [SIL-2] Use psutil to kill the entire process tree reliably
            parent = psutil.Process(handle.pid)
            procs = parent.children(recursive=True) + [parent]

            for p in procs:
                with contextlib.suppress(psutil.NoSuchProcess, ProcessLookupError):
                    p.terminate()

            # [SIL-2] Unified wait and force-kill delegation
            _, alive = psutil.wait_procs(procs, timeout=0.5)
            for p in alive:
                with contextlib.suppress(psutil.NoSuchProcess, ProcessLookupError):
                    logger.warning("Force killing zombie child process %d", p.pid)
                    p.kill()

            with contextlib.suppress(ProcessLookupError, OSError, RuntimeError, AttributeError):
                handle.terminate()
        except (psutil.NoSuchProcess, ProcessLookupError):
            pass
        except (psutil.AccessDenied, OSError, RuntimeError, ValueError) as e:
            logger.error("Error stopping process %d: %s", pid, e)
            return False

        wait_fn = getattr(handle, "wait", None)
        if callable(wait_fn):
            try:
                wait_result = wait_fn()
                if inspect.isawaitable(wait_result):
                    await asyncio.wait_for(wait_result, timeout=1.0)
            except asyncio.TimeoutError:
                with contextlib.suppress(ProcessLookupError, OSError, RuntimeError, AttributeError):
                    handle.kill()
                try:
                    wait_result = wait_fn()
                    if inspect.isawaitable(wait_result):
                        await asyncio.wait_for(wait_result, timeout=1.0)
                except (
                    asyncio.TimeoutError,
                    ProcessLookupError,
                    OSError,
                    RuntimeError,
                    ValueError,
                ):
                    logger.warning("Timed out waiting for process %d to exit cleanly", pid)
            except (ProcessLookupError, OSError, RuntimeError, ValueError):
                pass

        async with self.state.process_lock:
            current = self.state.running_processes.get(pid)
            if current is not None and current is proc_entry:
                current.exit_code = getattr(handle, "returncode", None)

        await self._finalize_process(pid)
        return True

    async def publish_poll_result(
        self,
        pid: int,
        batch: ProcessOutputBatch,
        inbound: Message | None = None,
    ) -> None:
        """Publish process output batch to MQTT."""
        response_topic = topic_path(
            self.state.mqtt_topic_prefix,
            Topic.SHELL,
            protocol.ShellAction.POLL,
            str(pid),
            protocol.MQTT_SUFFIX_RESPONSE,
        )

        reply_topic = None
        correlation_data = None
        if inbound and inbound.properties:
            reply_topic = getattr(inbound.properties, "ResponseTopic", None)
            correlation_data = getattr(inbound.properties, "CorrelationData", None)

        await self.mqtt_flow.enqueue_mqtt(
            QueuedPublish(
                topic_name=response_topic,
                payload=_msgpack_enc.encode(batch),
                content_type="application/msgpack",
                response_topic=reply_topic,
                correlation_data=correlation_data,
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
