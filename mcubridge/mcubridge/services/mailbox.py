"""Mailbox service implementation for asynchronous message passing."""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING


if TYPE_CHECKING:
    from mcubridge.config.settings import RuntimeConfig
    from mcubridge.state.context import RuntimeState
    from .base import BridgeContext
    from .runtime import BridgeService

logger = logging.getLogger("mcubridge.mailbox")


class MailboxComponent:
    """Handles bidirectional asynchronous message exchange."""

    def __init__(self, state: RuntimeState, config: RuntimeConfig, service: BridgeService) -> None:
        self.state = state
        self.config = config
        self.service = service

    async def handle_mqtt_command(self, ctx: BridgeContext) -> bool:
        return False

    async def handle_mqtt(self, ctx: BridgeContext) -> bool:
        return True

    async def handle_mcu_command(self, command_id: int, payload: bytes) -> bool:
        return False

    async def handle_push(self, payload: bytes) -> None:
        pass

    async def handle_available(self, payload: bytes) -> None:
        pass

    async def handle_read(self, payload: bytes) -> None:
        pass

    async def handle_processed(self, payload: bytes) -> None:
        pass
