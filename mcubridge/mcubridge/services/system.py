"""System service implementation for MCU status and configuration."""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING


if TYPE_CHECKING:
    from mcubridge.config.settings import RuntimeConfig
    from mcubridge.state.context import RuntimeState
    from .base import BridgeContext
    from .runtime import BridgeService

logger = logging.getLogger("mcubridge.system")


class SystemComponent:
    """Handles system-level commands like memory and version reports."""

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

    async def handle_get_free_memory_resp(self, payload: bytes) -> None:
        pass

    async def handle_get_version_resp(self, payload: bytes) -> None:
        pass

    async def handle_set_baudrate_resp(self, payload: bytes) -> None:
        pass
