"""Shell MQTT component coordinating with ProcessComponent."""

from __future__ import annotations

import logging
from contextlib import AsyncExitStack
from typing import Any, cast, Annotated

import msgspec
from aiomqtt.message import Message

from mcubridge.protocol import protocol
from mcubridge.protocol.protocol import ShellAction, Status, UINT16_MAX

from ..mqtt.messages import QueuedPublish
from ..config.settings import RuntimeConfig
from ..config.const import MQTT_EXPIRY_SHELL
from ..state.context import RuntimeState
from ..protocol.topics import Topic, topic_path
from ..policy import CommandValidationError
from .base import BridgeContext
from .process import ProcessComponent

# Constraints for msgspec validation
_MAX_COMMAND_LEN = 512


class PayloadValidationError(ValueError):
    """Raised when an inbound MQTT payload cannot be validated."""

    def __init__(self, message: str) -> None:
        super().__init__(message)
        self.message = message


class ShellCommandPayload(msgspec.Struct, frozen=True):
    """Represents a shell command request coming from MQTT.

    Accepts either plain text or JSON: {"command": "..."}.
    """

    command: Annotated[str, msgspec.Meta(min_length=1, max_length=_MAX_COMMAND_LEN)]

    @classmethod
    def from_mqtt(cls, payload: bytes) -> ShellCommandPayload:
        """Parse MQTT payload into a validated ShellCommandPayload."""
        text = payload.decode("utf-8", errors="ignore").strip()
        if not text:
            raise PayloadValidationError("Shell command payload is empty")

        # Accept both plain text and JSON format
        if text.startswith("{"):
            try:
                result = msgspec.json.decode(text, type=cls)
                # Normalize whitespace
                normalized = result.command.strip()
                if not normalized:
                    raise PayloadValidationError("Shell command payload is empty")
                return cls(command=normalized)
            except msgspec.ValidationError as exc:
                raise PayloadValidationError(str(exc)) from exc
            except msgspec.DecodeError:
                # Malformed JSON - treat entire text as command
                pass

        # Plain text command
        if len(text) > _MAX_COMMAND_LEN:
            raise PayloadValidationError("Command cannot exceed 512 characters")
        return cls(command=text)


class ShellPidPayload(msgspec.Struct, frozen=True):
    """MQTT payload specifying an async shell PID to operate on."""

    pid: Annotated[int, msgspec.Meta(gt=0, le=UINT16_MAX)]

    @classmethod
    def from_topic_segment(cls, segment: str) -> ShellPidPayload:
        """Parse a topic segment into a validated ShellPidPayload."""
        try:
            value = int(segment, 10)
        except ValueError as exc:
            raise PayloadValidationError("PID segment must be an integer") from exc

        # Validate constraints manually since msgspec.Struct only validates during decode
        if value <= 0:
            raise PayloadValidationError("PID must be a positive integer")
        if value > UINT16_MAX:
            raise PayloadValidationError("PID cannot exceed 65535")

        return cls(pid=value)


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
                QueuedPublish(
                    topic_name=response_topic,
                    payload=b"Error: shell handler failed unexpectedly",
                    content_type="text/plain; charset=utf-8",
                    message_expiry_interval=MQTT_EXPIRY_SHELL,
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
                QueuedPublish(
                    topic_name=response_topic,
                    payload=response.encode("utf-8"),
                    content_type="text/plain; charset=utf-8",
                    message_expiry_interval=MQTT_EXPIRY_SHELL,
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
                QueuedPublish(
                    topic_name=response_topic,
                    payload=b"error:not_allowed",
                ),
                reply_context=inbound,
            )
            return

        await self.ctx.enqueue_mqtt(
            QueuedPublish(
                topic_name=response_topic,
                payload=str(pid).encode("utf-8"),
            ),
            reply_context=inbound,
        )

    async def _handle_poll(self, pid_model: ShellPidPayload) -> None:
        pid = pid_model.pid

        batch = await self.process.collect_output(pid)

        await self.process.publish_poll_result(pid, batch)

    async def _handle_kill(self, pid_model: ShellPidPayload) -> None:
        await self.process.handle_kill(
            cast(Any, protocol.UINT16_STRUCT).build(pid_model.pid),
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
