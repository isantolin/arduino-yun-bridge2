"""Shell MQTT component coordinating with ProcessComponent."""

from __future__ import annotations

import logging
from contextlib import AsyncExitStack

from aiomqtt.message import Message

from mcubridge.rpc import protocol
from mcubridge.rpc.protocol import ShellAction, Status

from ...mqtt.messages import QueuedPublish
from ...config.settings import RuntimeConfig
from ...state.context import RuntimeState
from ...protocol.topics import Topic, topic_path
from ...policy import CommandValidationError
from ..payloads import (
    PayloadValidationError,
    ShellCommandPayload,
    ShellPidPayload,
)
from .base import BridgeContext
from .process import ProcessComponent

logger = logging.getLogger("mcubridge.shell")


class ShellComponent:
    """Handle shell-related MQTT topics and responses."""

    def __init__(
        self,
        config: RuntimeConfig,
        state: RuntimeState,
        ctx: BridgeContext,
        process: ProcessComponent,
    ) -> None:
        self.config = config
        self.state = state
        self.ctx = ctx
        self.process = process

    async def handle_mqtt(
        self,
        parts: list[str],
        payload: bytes,
        inbound: Message | None = None,
    ) -> None:
        action = parts[2] if len(parts) >= 3 else ""

        match action:
            case ShellAction.RUN:
                payload_model = self._parse_shell_command(payload, action)
                if payload_model is None:
                    return
                await self._handle_shell_run(payload_model, inbound)

            case ShellAction.RUN_ASYNC:
                payload_model = self._parse_shell_command(payload, action)
                if payload_model is None:
                    return
                await self._handle_run_async(payload_model, inbound)

            case ShellAction.POLL if len(parts) == 4:
                pid_model = self._parse_shell_pid(parts[3], action)
                if pid_model is None:
                    return
                await self._handle_poll(pid_model)

            case ShellAction.KILL if len(parts) == 4:
                pid_model = self._parse_shell_pid(parts[3], action)
                if pid_model is None:
                    return
                await self._handle_kill(pid_model)

            case _:
                logger.debug(
                    "Ignoring shell topic action: %s",
                    "/".join(parts),
                )

    async def _handle_shell_run(
        self,
        payload: ShellCommandPayload,
        inbound: Message | None,
    ) -> None:
        command = payload.command
        logger.info("Executing shell command from MQTT: '%s'", command)
        response_topic = topic_path(
            self.state.mqtt_topic_prefix,
            Topic.SHELL,
            protocol.MQTT_SUFFIX_RESPONSE,
        )

        async with AsyncExitStack() as stack:
            stack.push_async_callback(
                self.ctx.enqueue_mqtt,
                self._build_response(
                    response_topic,
                    b"Error: shell handler failed unexpectedly",
                    content_type="text/plain; charset=utf-8",
                    message_expiry_interval=30,
                ),
                reply_context=inbound,
            )
            (
                status,
                stdout_bytes,
                stderr_bytes,
                exit_code,
            ) = await self.process.run_sync(command)

            stdout_text = stdout_bytes.decode("utf-8", errors="ignore")
            stderr_text = stderr_bytes.decode("utf-8", errors="ignore")

            if status == Status.OK.value:
                response = (
                    "Exit Code: "
                    f"{exit_code if exit_code is not None else 'unknown'}\n"
                    f"-- STDOUT --\n{stdout_text}\n-- STDERR --\n{stderr_text}"
                )
            elif status == Status.TIMEOUT.value:
                response = "Error: Command timed out after " f"{self.state.process_timeout} seconds."
            elif status == Status.MALFORMED.value:
                response = "Error: Empty command"
            else:
                error_detail = stderr_text or "Unexpected server error"
                response = f"Error: {error_detail}"

            stack.pop_all()
            await self.ctx.enqueue_mqtt(
                self._build_response(
                    response_topic,
                    response.encode("utf-8"),
                    content_type="text/plain; charset=utf-8",
                    message_expiry_interval=30,
                ),
                reply_context=inbound,
            )

    async def _handle_run_async(
        self,
        payload: ShellCommandPayload,
        inbound: Message | None,
    ) -> None:
        command = payload.command
        logger.info("MQTT async shell command: '%s'", command)
        try:
            pid = await self.process.start_async(command)
        except CommandValidationError as exc:
            response_topic = topic_path(
                self.state.mqtt_topic_prefix,
                Topic.SHELL,
                ShellAction.RUN_ASYNC,
                "error",
            )
            await self.ctx.enqueue_mqtt(
                QueuedPublish(
                    topic_name=response_topic,
                    payload=f"error:{exc.message}".encode(),
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

        if pid == protocol.INVALID_ID_SENTINEL:
            await self.ctx.enqueue_mqtt(
                self._build_response(response_topic, b"error:not_allowed"),
                reply_context=inbound,
            )
            return

        await self.ctx.enqueue_mqtt(
            self._build_response(response_topic, str(pid).encode("utf-8")),
            reply_context=inbound,
        )

    def _build_response(
        self,
        topic_name: str,
        payload: bytes,
        *,
        content_type: str | None = None,
        message_expiry_interval: int | None = None,
    ) -> QueuedPublish:
        return QueuedPublish(
            topic_name=topic_name,
            payload=payload,
            content_type=content_type,
            message_expiry_interval=message_expiry_interval,
        )

    async def _handle_poll(self, pid_model: ShellPidPayload) -> None:
        pid = pid_model.pid

        batch = await self.process.collect_output(pid)

        await self.process.publish_poll_result(pid, batch)

    async def _handle_kill(self, pid_model: ShellPidPayload) -> None:
        await self.process.handle_kill(
            protocol.UINT16_STRUCT.build(pid_model.pid),
            send_ack=False,
        )

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


__all__ = ["ShellComponent"]
