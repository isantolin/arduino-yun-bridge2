"""Extra tests for ConsoleComponent edges."""

from __future__ import annotations

import os
import time
from unittest.mock import AsyncMock, MagicMock, patch

import msgspec
import pytest
from mcubridge.config.settings import RuntimeConfig
from mcubridge.protocol.structures import ConsoleWritePacket
from mcubridge.services.console import ConsoleComponent
from mcubridge.state.context import create_runtime_state


@pytest.mark.asyncio
async def test_console_handle_write_edge_cases() -> None:
    config = RuntimeConfig(
        serial_shared_secret=b"secret_1234",
        mqtt_spool_dir=f".tmp_tests/mcubridge-test-console-extra-{os.getpid()}-{time.time_ns()}",
    )
    state = create_runtime_state(config)
    try:
        serial_flow = MagicMock()
        serial_flow.send = AsyncMock(return_value=True)

        comp = ConsoleComponent(
            config=config, state=state, serial_flow=serial_flow
        )

        with patch("mcubridge.services.console.atomic_publish", new_callable=AsyncMock) as mock_publish:
            # 1. Malformed payload
            await comp.handle_write(0, b"\xff\xff")
            assert not mock_publish.called

            # 2. Empty data in packet
            empty_pkt = msgspec.msgpack.encode(ConsoleWritePacket(data=b""))
            await comp.handle_write(1, empty_pkt)
            assert not mock_publish.called

            # 3. Successful write
            valid_pkt = msgspec.msgpack.encode(ConsoleWritePacket(data=b"hello"))
            await comp.handle_write(2, valid_pkt)
            assert mock_publish.called
    finally:
        if os.path.exists(config.mqtt_spool_dir):
            import shutil
            shutil.rmtree(config.mqtt_spool_dir, ignore_errors=True)
