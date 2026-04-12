"""Extra coverage for mcubridge.services.console."""

from unittest.mock import AsyncMock, MagicMock

import pytest
from mcubridge.config.settings import RuntimeConfig
from mcubridge.services.console import ConsoleComponent
from mcubridge.state.context import create_runtime_state


@pytest.mark.asyncio
async def test_console_handle_write_edge_cases() -> None:
    import os
    import time

    config = RuntimeConfig(
        serial_shared_secret=b"secret_1234",
        mqtt_spool_dir=f"/tmp/mcubridge-test-console-{os.getpid()}-{time.time_ns()}",
    )
    state = create_runtime_state(config)
    try:
        ctx = MagicMock()
        ctx.publish = AsyncMock()
        cc = ConsoleComponent(config, state, ctx)

        # Malformed
        await cc.handle_write(0, b"")
        assert ctx.publish.call_count == 0

        # Empty data (decoded from valid but empty packet)
        from mcubridge.protocol.structures import ConsoleWritePacket

        payload = ConsoleWritePacket(data=b"").encode()
        await cc.handle_write(0, payload)
        assert ctx.publish.call_count == 0
    finally:
        state.cleanup()


@pytest.mark.asyncio
async def test_console_mqtt_input_send_fail() -> None:
    import os
    import time

    config = RuntimeConfig(
        serial_shared_secret=b"secret_1234",
        mqtt_spool_dir=f"/tmp/mcubridge-test-console-{os.getpid()}-{time.time_ns()}",
    )
    state = create_runtime_state(config)
    try:
        ctx = MagicMock()
        ctx.send_frame = AsyncMock(return_value=False)
        cc = ConsoleComponent(config, state, ctx)

        # Send fails, should queue remaining
        await cc.handle_mqtt_input(  # type: ignore[reportPrivateUsage]
            b"chunk1chunk2"
        )  # pyright: ignore[reportPrivateUsage]
        assert len(state.console_to_mcu_queue) > 0
    finally:
        state.cleanup()


@pytest.mark.asyncio
async def test_console_flush_queue_send_fail() -> None:
    import os
    import time

    config = RuntimeConfig(
        serial_shared_secret=b"secret_1234",
        mqtt_spool_dir=f"/tmp/mcubridge-test-console-{os.getpid()}-{time.time_ns()}",
    )
    state = create_runtime_state(config)
    try:
        ctx = MagicMock()
        ctx.send_frame = AsyncMock(return_value=False)
        cc = ConsoleComponent(config, state, ctx)

        state.enqueue_console_chunk(b"hello")
        await cc.flush_queue()
        assert len(state.console_to_mcu_queue) > 0
    finally:
        state.cleanup()
