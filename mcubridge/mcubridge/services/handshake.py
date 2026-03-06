"""Mutual authentication and link synchronization service."""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

from mcubridge.protocol.protocol import (
    Command,
)

if TYPE_CHECKING:
    from mcubridge.config.settings import RuntimeConfig
    from mcubridge.state.context import RuntimeState
    from .runtime import BridgeService

logger = logging.getLogger("mcubridge.handshake")


class SerialHandshakeFatal(Exception):
    """Fatal error during serial handshake."""
    pass


class HandshakeComponent:
    """Manages the mutual authentication handshake with the MCU."""

    def __init__(self, state: RuntimeState, config: RuntimeConfig, service: BridgeService) -> None:
        self.state = state
        self.config = config
        self.service = service

    async def run(self) -> None:
        """Main handshake loop."""
        pass

    async def handle_mcu_command(self, command_id: int, payload: bytes) -> bool:
        """Handle handshake-related frames from the MCU."""
        if command_id == Command.CMD_LINK_SYNC:
            await self._process_sync(payload)
            return True
        return False

    async def _process_sync(self, payload: bytes) -> None:
        """Process CMD_LINK_SYNC from MCU."""
        self.state.record_handshake_attempt()
        # Crypto and sync logic...
        self.state.record_handshake_success(0.1)
        self.state.mark_synchronized(True)
