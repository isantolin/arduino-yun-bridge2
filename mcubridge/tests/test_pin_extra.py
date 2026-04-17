
"""Extra coverage for mcubridge.services.pin."""

from unittest.mock import AsyncMock, MagicMock

import pytest
from mcubridge.config.settings import RuntimeConfig
from mcubridge.protocol.protocol import Topic
from mcubridge.services.pin import PinComponent
from mcubridge.state.context import McuCapabilities, create_runtime_state


@pytest.mark.asyncio
async def test_pin_handle_read_overflow() -> None:
    import os
    import time

    config = RuntimeConfig(
        serial_shared_secret=b"secret_1234",
        file_system_root=f"/tmp/mcubridge-test-{os.getpid()}-{time.time_ns()}",
    )
    state = create_runtime_state(config)
    try:
        state.pending_pin_request_limit = 1
        ctx = MagicMock()
        ctx.publish = AsyncMock()
        ctx.send_frame = AsyncMock(return_value=True)
        pc = PinComponent(config, state, ctx)

        # Fill queue
        await getattr(pc, "_handle_read_command")(Topic.DIGITAL, 13, None)
        assert len(state.pending_digital_reads) == 1

        # Overflow
        await getattr(pc, "_handle_read_command")(Topic.DIGITAL, 13, None)
        ctx.publish.assert_called()
        assert ("bridge-error", "pending-pin-overflow") in ctx.publish.call_args[1][
            "properties"
        ]
    finally:
        state.cleanup()


@pytest.mark.asyncio
async def test_pin_handle_read_send_fail() -> None:
    import os
    import time

    config = RuntimeConfig(
        serial_shared_secret=b"secret_1234",
        file_system_root=f"/tmp/mcubridge-test-{os.getpid()}-{time.time_ns()}",
    )
    state = create_runtime_state(config)
    try:
        ctx = MagicMock()
        ctx.send_frame = AsyncMock(return_value=False)
        pc = PinComponent(config, state, ctx)

        await getattr(pc, "_handle_read_command")(Topic.DIGITAL, 13, None)
        assert len(state.pending_digital_reads) == 0
    finally:
        state.cleanup()


@pytest.mark.asyncio
async def test_pin_handle_mode_invalid() -> None:
    import os
    import time

    config = RuntimeConfig(
        serial_shared_secret=b"secret_1234",
        file_system_root=f"/tmp/mcubridge-test-{os.getpid()}-{time.time_ns()}",
    )
    state = create_runtime_state(config)
    try:
        pc = PinComponent(config, state, MagicMock())

        # Invalid int
        await getattr(pc, "_handle_mode_command")(13, "13", "not_an_int")

        # Invalid mode
        await getattr(pc, "_handle_mode_command")(13, "13", "5")
    finally:
        state.cleanup()


@pytest.mark.asyncio
async def test_pin_validate_access_block() -> None:
    import os
    import time

    config = RuntimeConfig(
        serial_shared_secret=b"secret_1234",
        file_system_root=f"/tmp/mcubridge-test-{os.getpid()}-{time.time_ns()}",
    )
    state = create_runtime_state(config)
    try:
        state.mcu_capabilities = McuCapabilities(
            protocol_version=2,
            board_arch=1,
            num_digital_pins=20,
            num_analog_inputs=6,
            features={},
        )
        pc = PinComponent(config, state, MagicMock())

        assert (
            getattr(pc, "_validate_pin_access")(25, False)
            is False
        )  # Digital limit 20
        assert (
            getattr(pc, "_validate_pin_access")(10, True)
            is False
        )  # Analog limit 6
    finally:
        state.cleanup()
