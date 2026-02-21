"""Extra coverage for mcubridge.services.console."""

from unittest.mock import AsyncMock, MagicMock

import pytest
from mcubridge.config.settings import RuntimeConfig
from mcubridge.services.console import ConsoleComponent
from mcubridge.state.context import create_runtime_state


@pytest.mark.asyncio
async def test_console_handle_write_edge_cases() -> None:
    config = RuntimeConfig(serial_shared_secret=b"secret_1234")
    state = create_runtime_state(config)
    ctx = MagicMock()
    ctx.publish = AsyncMock()
    cc = ConsoleComponent(config, state, ctx)

    # Malformed
    await cc.handle_write(b"")
    assert ctx.publish.call_count == 0

    # Empty data (decoded from valid but empty packet)
    from mcubridge.protocol.structures import ConsoleWritePacket
    payload = ConsoleWritePacket(data=b"").encode()
    await cc.handle_write(payload)
    assert ctx.publish.call_count == 0


@pytest.mark.asyncio
async def test_console_mqtt_input_send_fail() -> None:
    config = RuntimeConfig(serial_shared_secret=b"secret_1234")
    state = create_runtime_state(config)
    ctx = MagicMock()
    ctx.send_frame = AsyncMock(return_value=False)
    cc = ConsoleComponent(config, state, ctx)

    # Send fails, should queue remaining
    await cc.handle_mqtt_input(b"chunk1chunk2")
    assert len(state.console_to_mcu_queue) > 0


@pytest.mark.asyncio
async def test_console_flush_queue_send_fail() -> None:
    config = RuntimeConfig(serial_shared_secret=b"secret_1234")
    state = create_runtime_state(config)
    ctx = MagicMock()
    ctx.send_frame = AsyncMock(return_value=False)
    cc = ConsoleComponent(config, state, ctx)

    state.enqueue_console_chunk(b"hello", MagicMock())
    await cc.flush_queue()
    assert len(state.console_to_mcu_queue) > 0
