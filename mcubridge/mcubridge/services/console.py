"""Console service implementation for serial terminal exchange."""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING


if TYPE_CHECKING:
    from mcubridge.config.settings import RuntimeConfig
    from mcubridge.state.context import RuntimeState
    from .base import BridgeContext
    from .runtime import BridgeService

logger = logging.getLogger("mcubridge.console")


class ConsoleComponent:
    """Handles serial console input/output via MQTT."""

    def __init__(self, state: RuntimeState, config: RuntimeConfig, service: BridgeService) -> None:
        self.state = state
        self.config = config
        self.service = service

    async def handle_mqtt_command(self, ctx: BridgeContext) -> bool:
        return False

    async def handle_mqtt_input(self, ctx: BridgeContext) -> bool:
        return True

    async def handle_mcu_command(self, command_id: int, payload: bytes) -> bool:
        return False

    async def handle_xoff(self, payload: bytes) -> None:
        pass

    async def handle_xon(self, payload: bytes) -> None:
        pass

    async def handle_write(self, payload: bytes) -> None:
        pass
