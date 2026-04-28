from __future__ import annotations

import asyncio
import contextlib
import structlog
from collections.abc import Awaitable, Callable
from typing import TYPE_CHECKING

import msgspec
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
    RuntimeState,
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
            await self.mqtt_flow.enqueue_mqtt(
                QueuedPublish(
                    topic_name=response_topic,
                    payload=b"error:internal",
                ),
                reply_context=inbound,
            )
            return

        response_topic = topic_path(
            self.state.mqtt_topic_prefix,
            Topic.SHELL,
            ShellAction.RUN_ASYNC,
            protocol.MQTT_SUFFIX_RESPONSE,
        )

        if pid == 0:
            await self.mqtt_flow.enqueue_mqtt(
                QueuedPublish(
                    topic_name=response_topic,
                    payload=b"error:not_allowed_or_limit_reached",
                ),
                reply_context=inbound,
            )
            return

        await self.mqtt_flow.enqueue_mqtt(
            QueuedPublish(
                topic_name=response_topic,
                payload=str(pid).encode("utf-8"),
            ),
            reply_context=inbound,
        )

    async def _handle_mqtt_poll(
        self, pid_model: ShellPidPayload, inbound: Message | None = None
    ) -> None:
        pid = pid_model.pid
        batch = await self.poll_process(pid)
        await self.publish_poll_result(pid, batch, inbound)

    async def _handle_mqtt_kill(
        self, pid_model: ShellPidPayload, inbound: Message | None = None
    ) -> None:
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
            packet = msgspec.msgpack.decode(
                payload, type=structures.ProcessRunAsyncPacket
            )
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
                resp = msgspec.msgpack.encode(
                    structures.ProcessRunAsyncResponsePacket(pid=pid)
                )
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
        except (
            msgspec.ValidationError,
            msgspec.DecodeError,
            ValueError,
            AttributeError,
        ):
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

    async def handle_kill(
        self, seq_id: int, payload: bytes, *, send_ack: bool = True
    ) -> bool:
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

            async with self.state.process_lock:
                self.state.running_processes[pid] = process
                self.state.process_io_locks[pid] = asyncio.Lock()
                self.state.process_exit_codes[pid] = 0

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
                handle = self.state.running_processes.get(pid)

            if handle:
                # [SIL-2] Non-blocking wait for process exit
                try:
                    exit_code = await asyncio.wait_for(
                        handle.wait(), timeout=float(self.state.process_timeout)
                    )
                    self.state.process_exit_codes[pid] = exit_code
                except asyncio.TimeoutError:
                    logger.warning(
                        "Process %d monitor timed out; forcing finalization", pid
                    )
                    with contextlib.suppress(OSError):
                        handle.kill()
                    self.state.process_exit_codes[pid] = -1
        finally:
            self._finalize_process_internal(pid)

    def _is_drained(self, handle: asyncio.subprocess.Process | None) -> bool:
        """[SIL-2] Non-blocking EOF check using native library state."""
        if not handle:
            return True

        # Process is considered drained only if it's finished and IO is EOF
        if handle.returncode is None:
            return False

        out_eof = getattr(handle.stdout, "at_eof", lambda: True)()
        err_eof = getattr(handle.stderr, "at_eof", lambda: True)()
        return out_eof and err_eof

    async def poll_process(self, pid: int) -> ProcessOutputBatch:
        """Fetch pending output and status for a running process."""
        async with self.state.process_lock:
            handle = self.state.running_processes.get(pid)
            io_lock = self.state.process_io_locks.get(pid)
            exit_code = self.state.process_exit_codes.get(pid, 0)

            if not handle or not io_lock:
                return ProcessOutputBatch(
                    Status.ERROR.value, 1, b"", b"", True, False, False
                )

            async with io_lock:
                budget = protocol.MAX_PAYLOAD_SIZE - 32

                async def _read_stream(
                    stream: asyncio.StreamReader | None,
                ) -> tuple[bytes, bool]:
                    if not stream or stream.at_eof():
                        return b"", False
                    try:
                        # Use direct read natively instead of intermediate deque
                        chunk = await asyncio.wait_for(
                            stream.read(budget), timeout=0.01
                        )
                        return chunk, not stream.at_eof()
                    except asyncio.TimeoutError:
                        return b"", True

                stdout_chunk, t_out = await _read_stream(handle.stdout)
                stderr_chunk, t_err = await _read_stream(handle.stderr)

                is_finished = handle.returncode is not None

                batch = ProcessOutputBatch(
                    Status.OK.value,
                    exit_code,
                    stdout_chunk,
                    stderr_chunk,
                    is_finished,
                    t_out,
                    t_err,
                )
                if is_finished and self._is_drained(handle):
                    self._finalize_process_internal(pid)
                return batch

    async def stop_process(self, pid: int) -> bool:
        """Terminate a running process and its children recursively."""
        import psutil

        async with self.state.process_lock:
            handle = self.state.running_processes.get(pid)
            if not handle:
                return False

        # [SIL-2] Reliably terminate a process and all its children.
        # Uses psutil directly for atomic tree traversal and signal mapping.
        try:
            parent = psutil.Process(handle.pid)
            children = parent.children(recursive=True)
            all_procs = children + [parent]

            # 1. Terminate all
            for p in all_procs:
                with contextlib.suppress(psutil.NoSuchProcess, ProcessLookupError):
                    p.terminate()

            # 2. Wait for termination
            _, alive = psutil.wait_procs(all_procs, timeout=3.0)

            # 3. Force kill survivors
            for p in alive:
                with contextlib.suppress(psutil.NoSuchProcess, ProcessLookupError):
                    logger.warning("Force killing zombie process %d", p.pid)
                    p.kill()

        except (psutil.NoSuchProcess, ProcessLookupError):
            pass
        except (psutil.Error, RuntimeError) as e:
            logger.error(
                "Error during process tree cleanup (pid=%d): %s",
                handle.pid,
                e,
                exc_info=True,
            )

        # Update exit code manually since psutil logic above is synchronous
        # but the actual process object (handle) might still need its state updated
        # in the asyncio loop.
        async with self.state.process_lock:
            if pid in self.state.running_processes:
                self.state.process_exit_codes[pid] = getattr(handle, "returncode", -1)

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
        handle = self.state.running_processes.pop(pid, None)
        self.state.process_io_locks.pop(pid, None)
        # Note: We keep the exit code in self.state.process_exit_codes until
        # specifically cleared or overwritten, allowing asynchronous status
        # retrieval after process termination.
        if handle:
            self._process_slots.release()


__all__ = ["ProcessComponent", "ProcessOutputBatch"]
