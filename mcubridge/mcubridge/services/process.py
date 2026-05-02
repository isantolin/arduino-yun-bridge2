from __future__ import annotations

import asyncio
import structlog
from typing import TYPE_CHECKING, Any

import msgspec
from aiomqtt.message import Message

from ..protocol import protocol
from ..protocol.protocol import ShellAction, Status, Command
from ..protocol.structures import (
    QueuedPublish,
    ShellCommandPayload,
    ShellPidPayload,
    TopicRoute,
)
from ..protocol.topics import Topic, topic_path

if TYPE_CHECKING:
    from ..state.context import RuntimeState
    from ..config.settings import RuntimeConfig
    from .serial_flow import SerialFlowController

logger = structlog.get_logger("mcubridge.services.process")
_msgpack_enc = msgspec.msgpack.Encoder()


class ProcessComponent:
    """Component for managing subprocess execution and output capture. [SIL-2]"""

    def __init__(
        self,
        config: RuntimeConfig,
        state: RuntimeState,
        serial_flow: SerialFlowController,
        enqueue_mqtt: Any,
    ) -> None:
        self.config = config
        self.state = state
        self.serial_flow = serial_flow
        self.enqueue_mqtt = enqueue_mqtt

        # [SIL-2] Ensure numeric limit for semaphore
        limit = int(state.process_max_concurrent)
        self._process_slots = asyncio.Semaphore(limit)

    async def handle_run_async(self, seq_id: int, payload: bytes) -> bool:
        """Handle CMD_PROCESS_RUN_ASYNC from MCU."""
        try:
            packet = msgspec.msgpack.decode(payload, type=ShellCommandPayload)
            cmd = packet.command
        except (ValueError, msgspec.MsgspecError) as e:
            logger.warning("Malformed ShellCommandPayload: %s", e)
            return False

        pid = await self.run_async(cmd)
        resp = ShellPidPayload(pid=pid)
        return await self.serial_flow.send(
            Command.CMD_PROCESS_RUN_ASYNC_RESP.value, msgspec.msgpack.encode(resp)
        )

    async def handle_poll(self, seq_id: int, payload: bytes) -> bool:
        """Handle CMD_PROCESS_POLL from MCU."""
        try:
            packet = msgspec.msgpack.decode(payload, type=ShellPidPayload)
            pid = packet.pid
        except (ValueError, msgspec.MsgspecError) as e:
            logger.warning("Malformed ShellPidPayload: %s", e)
            return False

        exit_code = self.state.process_exit_codes.get(pid, -1)
        resp = msgspec.msgpack.encode({"pid": pid, "exit_code": exit_code})
        return await self.serial_flow.send(Command.CMD_PROCESS_POLL_RESP.value, resp)

    async def handle_kill(self, seq_id: int, payload: bytes) -> bool | None:
        """Handle CMD_PROCESS_KILL from MCU."""
        try:
            packet = msgspec.msgpack.decode(payload, type=ShellPidPayload)
            pid = packet.pid
        except (ValueError, msgspec.MsgspecError) as e:
            logger.warning("Malformed kill payload from MCU: %s", e)
            return False

        if await self.stop_process(pid):
            return await self.serial_flow.send(Status.OK.value, b"")
        return await self.serial_flow.send(Status.ERROR.value, b"")

    async def handle_mqtt(self, route: TopicRoute, inbound: Message) -> bool:
        """Process inbound MQTT requests for shell operations."""
        action = route.identifier
        try:
            match action:
                case ShellAction.RUN_ASYNC:
                    try:
                        payload = msgspec.convert(inbound.payload, bytes).decode()
                        pid = await self.run_async(payload, inbound)
                        await self._publish_pid(inbound, pid)
                        return True
                    except (ValueError, UnicodeDecodeError) as e:
                        logger.warning("Malformed MQTT shell run request: %s", e)
                        return True

                case ShellAction.KILL:
                    try:
                        payload = msgspec.convert(inbound.payload, bytes).decode()
                        pid = int(payload)
                        await self.stop_process(pid)
                        return True
                    except (ValueError, UnicodeDecodeError):
                        return False
                case _:
                    return False
        except Exception as e:
            logger.error("Error processing MQTT shell action %s: %s", action, e)
            await self.enqueue_mqtt(
                QueuedPublish(
                    topic_name=topic_path(
                        self.state.mqtt_topic_prefix,
                        Topic.SHELL,
                        action,
                        protocol.MQTT_SUFFIX_RESPONSE,
                    ),
                    payload=b"error:internal",
                ),
                reply_context=inbound,
            )
            return True
        return False

    async def run_async(self, command: str, inbound: Message | None = None) -> int:
        if self._process_slots.locked():
            logger.warning("Concurrency limit reached; rejecting process run.")
            return 0

        # [SIL-2] Shell security validation
        if not self.state.allowed_policy.is_allowed(command):
            logger.warning("Security Block: Command not allowed by policy: %s", command)
            return 0

        await self._process_slots.acquire()
        async with self.state.process_lock:
            pid = self.state.next_pid
            self.state.next_pid += 1

        asyncio.get_running_loop().create_task(
            self._supervise_process(pid, command, inbound)
        )
        return pid

    async def stop_process(self, pid: int) -> bool:
        async with self.state.process_lock:
            proc = self.state.running_processes.get(pid)
            if proc:
                proc.terminate()
                return True
        return False

    async def _supervise_process(
        self, pid: int, command: str, inbound: Message | None = None
    ) -> None:
        try:
            import shlex

            args = shlex.split(command)
            proc = await asyncio.create_subprocess_exec(
                *args,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.STDOUT,
            )

            async with self.state.process_lock:
                self.state.running_processes[pid] = proc

            # Start IO monitoring
            await self._monitor_output(pid, proc, inbound)

            exit_code = await proc.wait()
            async with self.state.process_lock:
                self.state.process_exit_codes[pid] = exit_code
                self.state.running_processes.pop(pid, None)

        except (OSError, RuntimeError) as e:
            logger.error("Failed to execute process %d: %s", pid, e)
            response_topic = topic_path(
                self.state.mqtt_topic_prefix,
                Topic.SHELL,
                ShellAction.RUN_ASYNC,
                "error",
            )
            await self.enqueue_mqtt(
                QueuedPublish(
                    topic_name=response_topic,
                    payload=b"error:internal",
                ),
                reply_context=inbound,
            )

        finally:
            self._process_slots.release()

    async def _monitor_output(
        self, pid: int, proc: asyncio.subprocess.Process, inbound: Message | None
    ) -> None:
        if proc.stdout is None:
            return

        batch_topic = topic_path(
            self.state.mqtt_topic_prefix,
            Topic.SHELL,
            ShellAction.RUN_ASYNC,
            "stdout",
            str(pid),
        )

        try:
            while True:
                # [SIL-2] Read with limit to prevent memory exhaustion
                line = await proc.stdout.readline()
                if not line:
                    break

                # [SIL-2] Using direct dict for ad-hoc async output
                payload = msgspec.msgpack.encode({"pid": pid, "data": line})
                await self.enqueue_mqtt(
                    QueuedPublish(
                        topic_name=batch_topic,
                        payload=payload,
                    ),
                    reply_context=inbound,
                )

        except (OSError, RuntimeError) as e:
            logger.error("IO error monitoring process %d: %s", pid, e)

    async def _publish_pid(self, inbound: Message, pid: int) -> None:
        response_topic = topic_path(
            self.state.mqtt_topic_prefix,
            Topic.SHELL,
            ShellAction.RUN_ASYNC,
            protocol.MQTT_SUFFIX_RESPONSE,
        )

        if pid == 0:
            await self.enqueue_mqtt(
                QueuedPublish(
                    topic_name=response_topic,
                    payload=b"error:not_allowed_or_limit_reached",
                ),
                reply_context=inbound,
            )
            return

        await self.enqueue_mqtt(
            QueuedPublish(
                topic_name=response_topic,
                payload=str(pid).encode("utf-8"),
            ),
            reply_context=inbound,
        )


__all__ = ["ProcessComponent"]
