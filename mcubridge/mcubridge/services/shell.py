"""Shell service implementation for remote command execution."""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

from mcubridge.protocol.protocol import ShellAction
from mcubridge.protocol.topics import Topic

from ..config.settings import RuntimeConfig
from ..state.context import RuntimeState
from .base import BridgeContext
from .payloads import (
    PayloadValidationError,
    ShellCommandPayload,
    ShellPidPayload,
)
from .process import ProcessComponent

if TYPE_CHECKING:
    from .runtime import BridgeService

logger = logging.getLogger("mcubridge.shell")


class ShellComponent:
    """Handles remote shell command execution via MQTT."""

    def __init__(self, state: RuntimeState, config: RuntimeConfig, service: BridgeService) -> None:
        self.state = state
        self.config = config
        self.service = service
        self.process = ProcessComponent(self.config, self.state, self.service)

    async def handle_mqtt_command(self, ctx: BridgeContext) -> bool:
        """Process an inbound MQTT shell command."""
        route = ctx.route
        if route.topic != Topic.SHELL:
            return False

        action_str = route.identifier.upper()
        try:
            action = ShellAction[f"ACT_{action_str}"]
        except KeyError:
            return False

        if action == ShellAction.ACT_RUN:
            await self._handle_run(ctx, ShellAction.ACT_RUN)
            return True

        if action == ShellAction.ACT_RUN_ASYNC:
            await self._handle_run(ctx, ShellAction.ACT_RUN_ASYNC)
            return True

        if action == ShellAction.ACT_POLL:
            await self._handle_pid_action(ctx, ShellAction.ACT_POLL)
            return True

        if action == ShellAction.ACT_KILL:
            await self._handle_pid_action(ctx, ShellAction.ACT_KILL)
            return True

        return False

    async def _handle_run(self, ctx: BridgeContext, action: ShellAction) -> None:
        """Execute a shell command (Sync or Async)."""
        try:
            # Just validate the payload
            ShellCommandPayload.from_message(ctx.message)
            # Implementation details...
        except PayloadValidationError as exc:
            logger.warning("Invalid shell command payload: %s", exc)

    async def _handle_pid_action(self, ctx: BridgeContext, action: ShellAction) -> None:
        """Execute a PID-based action (Poll or Kill)."""
        try:
            # Just validate the payload
            ShellPidPayload.from_message(ctx.message)
            # Implementation details...
        except PayloadValidationError as exc:
            logger.warning("Invalid shell PID payload: %s", exc)

    async def handle_mcu_command(self, command_id: int, payload: bytes) -> bool:
        """Process an inbound serial shell response from the MCU."""
        return False
