"""Extra coverage for mcubridge.services.pin."""

from unittest.mock import AsyncMock, MagicMock

import pytest
from mcubridge.config.settings import RuntimeConfig
from mcubridge.protocol.protocol import Topic
from mcubridge.services.pin import PinComponent
from mcubridge.state.context import create_runtime_state, McuCapabilities


@pytest.mark.asyncio
async def test_pin_handle_read_overflow() -> None:
    config = RuntimeConfig(serial_shared_secret=b"secret_1234")
    state = create_runtime_state(config)
    state.pending_pin_request_limit = 1
    ctx = MagicMock()
    ctx.publish = AsyncMock()
    ctx.send_frame = AsyncMock(return_value=True)
    pc = PinComponent(config, state, ctx)

    # Fill queue
    await pc._handle_read_command(Topic.DIGITAL, 13, None)
    assert len(state.pending_digital_reads) == 1

    # Overflow
    await pc._handle_read_command(Topic.DIGITAL, 13, None)
    ctx.publish.assert_called()
    assert ("bridge-error", "pending-pin-overflow") in ctx.publish.call_args[1]["properties"]


@pytest.mark.asyncio
async def test_pin_handle_read_send_fail() -> None:
    config = RuntimeConfig(serial_shared_secret=b"secret_1234")
    state = create_runtime_state(config)
    ctx = MagicMock()
    ctx.send_frame = AsyncMock(return_value=False)
    pc = PinComponent(config, state, ctx)

    await pc._handle_read_command(Topic.DIGITAL, 13, None)
    assert len(state.pending_digital_reads) == 0


@pytest.mark.asyncio
async def test_pin_handle_mode_invalid() -> None:
    config = RuntimeConfig(serial_shared_secret=b"secret_1234")
    state = create_runtime_state(config)
    pc = PinComponent(config, state, MagicMock())

    # Invalid int
    await pc._handle_mode_command(13, "13", "not_an_int")

    # Invalid mode
    await pc._handle_mode_command(13, "13", "5")


@pytest.mark.asyncio
async def test_pin_validate_access_block() -> None:
    config = RuntimeConfig(serial_shared_secret=b"secret_1234")
    state = create_runtime_state(config)
    state.mcu_capabilities = McuCapabilities(
        protocol_version=2, board_arch=1,
        num_digital_pins=20, num_analog_inputs=6,
        features={}
    )
    pc = PinComponent(config, state, MagicMock())

    assert pc._validate_pin_access(25, False) is False # Digital limit 20
    assert pc._validate_pin_access(10, True) is False  # Analog limit 6
