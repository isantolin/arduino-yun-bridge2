"""Extra coverage for mcubridge.services.pin."""

from unittest.mock import AsyncMock, MagicMock

import pytest
from mcubridge.config.settings import RuntimeConfig
from mcubridge.protocol.protocol import Topic
from mcubridge.services.pin import PinComponent
from mcubridge.state.context import McuCapabilities, create_runtime_state


@pytest.mark.asyncio
async def test_pin_handle_read_overflow() -> None:
    import time
    import os

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
        await pc._handle_read_command(Topic.DIGITAL, 13, None)  # type: ignore[reportPrivateUsage]
        assert len(state.pending_digital_reads) == 1

        # Overflow
        await pc._handle_read_command(Topic.DIGITAL, 13, None)  # type: ignore[reportPrivateUsage]
        ctx.publish.assert_called()
        assert ("bridge-error", "pending-pin-overflow") in ctx.publish.call_args[1][
            "properties"
        ]
    finally:
        state.cleanup()


@pytest.mark.asyncio
async def test_pin_handle_read_send_fail() -> None:
    import time
    import os

    config = RuntimeConfig(
        serial_shared_secret=b"secret_1234",
        file_system_root=f"/tmp/mcubridge-test-{os.getpid()}-{time.time_ns()}",
    )
    state = create_runtime_state(config)
    try:
        ctx = MagicMock()
        ctx.send_frame = AsyncMock(return_value=False)
        pc = PinComponent(config, state, ctx)

        await pc._handle_read_command(Topic.DIGITAL, 13, None)  # type: ignore[reportPrivateUsage]
        assert len(state.pending_digital_reads) == 0
    finally:
        state.cleanup()


@pytest.mark.asyncio
async def test_pin_handle_mode_invalid() -> None:
    import time
    import os

    config = RuntimeConfig(
        serial_shared_secret=b"secret_1234",
        file_system_root=f"/tmp/mcubridge-test-{os.getpid()}-{time.time_ns()}",
    )
    state = create_runtime_state(config)
    try:
        pc = PinComponent(config, state, MagicMock())

        # Invalid int
        await pc._handle_mode_command(13, "13", "not_an_int")  # type: ignore[reportPrivateUsage]

        # Invalid mode
        await pc._handle_mode_command(13, "13", "5")  # type: ignore[reportPrivateUsage]
    finally:
        state.cleanup()


@pytest.mark.asyncio
async def test_pin_validate_access_block() -> None:
    import time
    import os

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
            features={},  # type: ignore[reportArgumentType]
        )
        pc = PinComponent(config, state, MagicMock())

        assert (
            pc.validate_pin_access(25, False)  # type: ignore[reportPrivateUsage]
            is False
        )  # Digital limit 20
        assert (
            pc.validate_pin_access(10, True)  # type: ignore[reportPrivateUsage]
            is False
        )  # Analog limit 6
    finally:
        state.cleanup()
