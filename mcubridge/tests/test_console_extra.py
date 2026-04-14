"""Extra coverage for mcubridge.services.console."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest
from mcubridge.config.settings import RuntimeConfig
from mcubridge.services.console import ConsoleComponent
from mcubridge.state.context import create_runtime_state


@pytest.mark.asyncio
async def test_console_on_serial_disconnected_resets_state() -> None:
    config = RuntimeConfig(serial_shared_secret=b"1234")
    state = create_runtime_state(config)
    state.mcu_is_paused = True
    state.serial_tx_allowed.clear()

    ctx = MagicMock()
    ctx.send_frame = AsyncMock()

    component = ConsoleComponent(config, state, ctx)
    component.on_serial_disconnected()

    assert state.mcu_is_paused is False
    assert state.serial_tx_allowed.is_set()
