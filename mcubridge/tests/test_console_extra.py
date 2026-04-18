"""Extra edge-case tests for ConsoleComponent (SIL-2)."""

from __future__ import annotations

import os
import time
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from mcubridge.config.settings import RuntimeConfig
from mcubridge.protocol.structures import ConsoleWritePacket
from mcubridge.services.console import ConsoleComponent
from mcubridge.state.context import create_runtime_state


@pytest.mark.asyncio
async def test_console_handle_write_edge_cases() -> None:
    config = RuntimeConfig(
        serial_shared_secret=b"secret_1234",
        mqtt_spool_dir=f"/tmp/mcubridge-test-console-{os.getpid()}-{time.time_ns()}",
    )
    state = create_runtime_state(config)
    try:
        ctx = MagicMock()
        ctx.serial_flow = MagicMock()
        ctx.serial_flow.send = AsyncMock(return_value=True)

        with patch("mcubridge.state.context.RuntimeState.publish", new_callable=AsyncMock) as mock_pub:
            comp = ConsoleComponent(config, state, ctx)

            # 1. Malformed payload
            await comp.handle_write(0, b"\xff\xff")
            mock_pub.assert_not_called()

            # 2. Empty data in packet
            empty_pkt = ConsoleWritePacket(data=b"").encode()
            await comp.handle_write(1, empty_pkt)
            mock_pub.assert_not_called()

            # 3. Successful write
            valid_pkt = ConsoleWritePacket(data=b"hello").encode()
            await comp.handle_write(2, valid_pkt)
            mock_pub.assert_called_once()
    finally:
        state.cleanup()


@pytest.mark.asyncio
async def test_console_mqtt_input_error_paths() -> None:
    config = RuntimeConfig(serial_shared_secret=b"secret_1234")
    state = create_runtime_state(config)
    try:
        ctx = MagicMock()
        ctx.serial_flow = MagicMock()
        # Simulate serial failure
        ctx.serial_flow.send = AsyncMock(return_value=False)

        comp = ConsoleComponent(config, state, ctx)

        # Sending input when serial fails should queue it
        await comp._handle_mqtt_input(b"lost-data")  # type: ignore[reportPrivateUsage]
        assert len(state.console_to_mcu_queue) == 1
    finally:
        state.cleanup()
