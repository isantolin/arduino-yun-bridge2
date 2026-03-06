"""Component for managing subprocess execution and output capture."""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING


if TYPE_CHECKING:
    from mcubridge.config.settings import RuntimeConfig
    from mcubridge.state.context import RuntimeState
    from .base import BridgeContext
    from .runtime import BridgeService

logger = logging.getLogger("mcubridge.process")


class ProcessComponent:
    """Component for managing subprocess execution and output capture."""

    def __init__(
        self,
        config: RuntimeConfig,
        state: RuntimeState,
        service: BridgeService,
    ) -> None:
        self.config = config
        self.state = state
        self.service = service

    async def handle_mqtt_command(self, ctx: BridgeContext) -> bool:
        return False

    async def handle_mqtt(self, ctx: BridgeContext) -> bool:
        return True

    async def handle_mcu_command(self, command_id: int, payload: bytes) -> bool:
        return False

    async def handle_run_async(self, payload: bytes) -> None:
        pass

    async def handle_poll(self, payload: bytes) -> None:
        pass

    async def handle_kill(self, payload: bytes) -> None:
        pass
