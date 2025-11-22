"""Shell MQTT component coordinating with ProcessComponent."""
from __future__ import annotations

import logging
from typing import Optional

from yunbridge.rpc.protocol import Status

from ...mqtt import InboundMessage, PublishableMessage
from ...config.settings import RuntimeConfig
from ...state.context import RuntimeState
from ...common import pack_u16
from ...protocol.topics import Topic, topic_path
from .base import BridgeContext
from .process import CommandValidationError, ProcessComponent

logger = logging.getLogger("yunbridge.shell")


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
        payload_str: str,
        inbound: Optional[InboundMessage] = None,
    ) -> None:
        action = parts[2] if len(parts) >= 3 else ""

        if action == "run":
            if not payload_str:
                return
            await self._handle_shell_run(payload_str, inbound)
            return

        if action == "run_async":
            if not payload_str:
                return
            await self._handle_run_async(payload_str, inbound)
            return

        if action == "poll" and len(parts) == 4:
            await self._handle_poll(parts[3])
            return

        if action == "kill" and len(parts) == 4:
            await self._handle_kill(parts[3])
            return

        logger.debug("Ignoring shell topic action: %s", "/".join(parts))

    async def _handle_shell_run(
        self,
        command: str,
        inbound: Optional[InboundMessage],
    ) -> None:
        logger.info("Executing shell command from MQTT: '%s'", command)
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
            response = (
                "Error: Command timed out after "
                f"{self.state.process_timeout} seconds."
            )
        elif status == Status.MALFORMED.value:
            response = "Error: Empty command"
        else:
            error_detail = stderr_text or "Unexpected server error"
            response = f"Error: {error_detail}"

        response_topic = topic_path(
            self.state.mqtt_topic_prefix,
            Topic.SHELL,
            "response",
        )
        base_message = (
            PublishableMessage(
                topic_name=response_topic,
                payload=response.encode("utf-8"),
            )
            .with_content_type("text/plain; charset=utf-8")
            .with_message_expiry(30)
        )
        await self.ctx.enqueue_mqtt(
            base_message,
            reply_context=inbound,
        )

    async def _handle_run_async(
        self,
        command: str,
        inbound: Optional[InboundMessage],
    ) -> None:
        logger.info("MQTT async shell command: '%s'", command)
        try:
            pid = await self.process.start_async(command)
        except CommandValidationError as exc:
            response_topic = topic_path(
                self.state.mqtt_topic_prefix,
                Topic.SHELL,
                "run_async",
                "error",
            )
            base_message = PublishableMessage(
                topic_name=response_topic,
                payload=f"error:{exc.message}".encode("utf-8"),
            )
            await self.ctx.enqueue_mqtt(
                base_message,
                reply_context=inbound,
            )
            return
        response_topic = topic_path(
            self.state.mqtt_topic_prefix,
            Topic.SHELL,
            "run_async",
            "response",
        )
        base_message = PublishableMessage(
            topic_name=response_topic,
            payload=b"",
        )
        if pid == 0xFFFF:
            await self.ctx.enqueue_mqtt(
                base_message.with_payload(b"error:not_allowed"),
                reply_context=inbound,
            )
            return
        await self.ctx.enqueue_mqtt(
            base_message.with_payload(str(pid).encode("utf-8")),
            reply_context=inbound,
        )

    async def _handle_poll(self, pid_str: str) -> None:
        try:
            pid = int(pid_str)
        except ValueError:
            logger.warning("Invalid MQTT PROCESS_POLL PID: %s", pid_str)
            return

        (
            status_byte,
            exit_code,
            stdout_buffer,
            stderr_buffer,
            finished,
            stdout_truncated,
            stderr_truncated,
        ) = await self.process.collect_output(pid)

        await self.process.publish_poll_result(
            pid,
            status_byte,
            exit_code,
            stdout_buffer,
            stderr_buffer,
            stdout_truncated,
            stderr_truncated,
            finished,
        )

    async def _handle_kill(self, pid_str: str) -> None:
        try:
            pid = int(pid_str)
        except ValueError:
            logger.warning("Invalid MQTT PROCESS_KILL PID: %s", pid_str)
            return
        await self.process.handle_kill(pack_u16(pid), send_ack=False)


__all__ = ["ShellComponent"]
