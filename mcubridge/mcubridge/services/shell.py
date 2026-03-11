"Shell MQTT component coordinating with ProcessComponent."

from __future__ import annotations

import logging

from aiomqtt.message import Message
from mcubridge.protocol import protocol
from mcubridge.protocol.protocol import ShellAction

from ..config.settings import RuntimeConfig
from ..protocol.topics import Topic, topic_path
from ..state.context import RuntimeState
from .base import BridgeContext
from .payloads import (
    PayloadValidationError,
    ShellCommandPayload,
    ShellPidPayload,
)
from .process import ProcessComponent

logger = logging.getLogger("mcubridge.shell")


class ShellComponent(BaseComponent):
    """Handle shell-related MQTT topics and responses."""

    def __init__(
        self,
        config: RuntimeConfig,
        state: RuntimeState,
        ctx: BridgeContext,
        process: ProcessComponent,
    ) -> None:
        super().__init__(config, state, ctx)
        self.process = process

    async def handle_mqtt(
        self,
        segments: list[str],
        payload: bytes,
        inbound: Message | None = None,
    ) -> None:
        if not segments:
            return

        action = segments[0]

        match action:
            case ShellAction.RUN_ASYNC:
                payload_model = self._parse_shell_command(payload, action)
                if payload_model is None:
                    return
                await self._handle_run_async(payload_model, inbound)

            case ShellAction.POLL if len(segments) == 2:
                pid_model = self._parse_shell_pid(segments[1], action)
                if pid_model is None:
                    return
                await self._handle_poll(pid_model)

            case ShellAction.KILL if len(segments) == 2:
                pid_model = self._parse_shell_pid(segments[1], action)
                if pid_model is None:
                    return
                await self._handle_kill(pid_model)

            case _:
                logger.debug(
                    "Ignoring shell topic action or unsupported sync run: %s",
                    "/".join(segments),
                )

    async def _handle_run_async(
        self,
        payload: ShellCommandPayload,
        inbound: Message | None,
    ) -> None:
        command = payload.command
        logger.info("MQTT async shell command: '%s'", command)
        try:
            # Policy is checked inside run_async as well, but we can check tokens here if needed for validation
            pid = await self.process.run_async(command)
        except (OSError, ValueError, RuntimeError) as exc:
            logger.error("Error starting async command: %s", exc)
            response_topic = topic_path(
                self.state.mqtt_topic_prefix,
                Topic.SHELL,
                ShellAction.RUN_ASYNC,
                "error",
            )
            await self.ctx.publish(
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
            await self.ctx.publish(
                topic=response_topic,
                payload=b"error:not_allowed_or_limit_reached",
                reply_to=inbound,
            )
            return

        await self.ctx.publish(
            topic=response_topic,
            payload=str(pid).encode("utf-8"),
            reply_to=inbound,
        )

    async def _handle_poll(self, pid_model: ShellPidPayload) -> None:
        pid = pid_model.pid
        batch = await self.process.poll_process(pid)
        await self.process.publish_poll_result(pid, batch)

    async def _handle_kill(self, pid_model: ShellPidPayload) -> None:
        await self.process.stop_process(pid_model.pid)

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
