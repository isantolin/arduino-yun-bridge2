"""Extra edge-case tests for ConsoleComponent (SIL-2)."""

from __future__ import annotations

import os
import time
from unittest.mock import AsyncMock, MagicMock

import pytest
from mcubridge.config.settings import RuntimeConfig
from mcubridge.protocol.structures import ConsoleWritePacket
from mcubridge.services.console import ConsoleComponent
from mcubridge.state.context import create_runtime_state


@pytest.mark.asyncio
async def test_console_handle_write_edge_cases() -> None:
    config = RuntimeConfig(
        serial_shared_secret=b"secret_1234",
        mqtt_spool_dir=f".tmp_tests/mcubridge-test-console-{os.getpid()}-{time.time_ns()}",
    )
    state = create_runtime_state(config)
    try:
        serial_flow = MagicMock()
        serial_flow.send = AsyncMock(return_value=True)
        mqtt_flow = MagicMock()
        mqtt_flow.publish = AsyncMock()

        comp = ConsoleComponent(
            config=config, state=state, serial_flow=serial_flow, mqtt_flow=mqtt_flow
        )

        # 1. Empty data in packet
        empty_pkt = ConsoleWritePacket(data=b"")
        await comp.handle_write(1, empty_pkt)
        assert not mqtt_flow.publish.called

        # 2. Successful write
        valid_pkt = ConsoleWritePacket(data=b"hello")
        await comp.handle_write(2, valid_pkt)
        assert mqtt_flow.publish.called
    finally:
        state.cleanup()


@pytest.mark.asyncio
async def test_console_mqtt_input_error_paths() -> None:
    config = RuntimeConfig(serial_shared_secret=b"secret_1234")
    state = create_runtime_state(config)
    try:
        serial_flow = MagicMock()
        # Simulate serial failure
        serial_flow.send = AsyncMock(return_value=False)
        mqtt_flow = MagicMock()

        comp = ConsoleComponent(
            config=config, state=state, serial_flow=serial_flow, mqtt_flow=mqtt_flow
        )

        # Sending input when serial fails should queue it
        await comp._handle_mqtt_input(b"lost-data")  # type: ignore[reportPrivateUsage]
        assert len(state.console_to_mcu_queue) == 1
    finally:
        state.cleanup()
