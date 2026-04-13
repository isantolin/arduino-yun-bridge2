import asyncio
from collections import deque
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from mcubridge.config.settings import RuntimeConfig
from mcubridge.protocol.protocol import MAX_PAYLOAD_SIZE, Command
from mcubridge.services.base import BridgeContext
from mcubridge.services.console import ConsoleComponent
from mcubridge.state.context import RuntimeState


@pytest.fixture
def console_component() -> ConsoleComponent:
    config = MagicMock(spec=RuntimeConfig)
    state = MagicMock(spec=RuntimeState)
    state.mqtt_topic_prefix = "bridge"
    state.mcu_is_paused = False
    state.serial_tx_allowed = asyncio.Event()
    state.serial_tx_allowed.set()
    state.console_to_mcu_queue = deque()

    ctx = AsyncMock(spec=BridgeContext)

    return ConsoleComponent(config, state, ctx)


@pytest.mark.asyncio
async def test_handle_write(console_component: ConsoleComponent) -> None:
    payload = b"console output"
    from mcubridge.protocol import structures

    await console_component.handle_write(
        0, structures.ConsoleWritePacket(data=payload).encode()
    )

    console_component.ctx.publish.assert_awaited_once()  # type: ignore[reportUnknownMemberType]
    call_args = console_component.ctx.publish.call_args  # type: ignore[reportUnknownVariableType]
    assert call_args.kwargs["topic"].endswith("console/out")  # type: ignore[reportUnknownMemberType]
    assert call_args.kwargs["payload"] == payload  # type: ignore[reportUnknownMemberType]


@pytest.mark.asyncio
async def test_flow_control(console_component: ConsoleComponent) -> None:
    # Initial state
    assert console_component.state.mcu_is_paused is False
    assert console_component.state.serial_tx_allowed.is_set() is True

    # XOFF
    await console_component.handle_xoff(0, b"")
    assert console_component.state.mcu_is_paused is True
    assert console_component.state.serial_tx_allowed.is_set() is False

    # XON
    with patch.object(
        console_component, "flush_queue", new_callable=AsyncMock
    ) as mock_flush:
        await console_component.handle_xon(0, b"")
        assert console_component.state.mcu_is_paused is False
        assert console_component.state.serial_tx_allowed.is_set() is True
        mock_flush.assert_awaited_once()


@pytest.mark.asyncio
async def test_handle_mqtt_input_direct(console_component: ConsoleComponent) -> None:
    payload = b"input"
    console_component.ctx.send_frame.return_value = True  # type: ignore[reportAttributeAccessIssue]

    await console_component._handle_mqtt_input(  # type: ignore[reportPrivateUsage]
        payload
    )  # pyright: ignore[reportPrivateUsage]

    from mcubridge.protocol import structures

    expected = structures.ConsoleWritePacket(data=payload).encode()
    console_component.ctx.send_frame.assert_awaited_once_with(  # type: ignore[reportUnknownMemberType]
        Command.CMD_CONSOLE_WRITE.value,
        expected,
    )


@pytest.mark.asyncio
async def test_handle_mqtt_input_paused(console_component: ConsoleComponent) -> None:
    console_component.state.mcu_is_paused = True
    payload = b"input"

    await console_component._handle_mqtt_input(  # type: ignore[reportPrivateUsage]
        payload
    )  # pyright: ignore[reportPrivateUsage]

    console_component.ctx.send_frame.assert_not_awaited()  # type: ignore[reportUnknownMemberType]
    # verify enqueue was called since state is a mock
    # The actual queue won't change because enqueue_console_chunk is a mock
    # We can't easily match the logger instance exactly without patching,
    # but we can check the payload.
    console_component.state.enqueue_console_chunk.assert_called_once()  # type: ignore[reportUnknownMemberType]
    args = console_component.state.enqueue_console_chunk.call_args  # type: ignore[reportUnknownVariableType]
    assert args[0][0] == payload


@pytest.mark.asyncio
async def test_handle_mqtt_input_chunking(console_component: ConsoleComponent) -> None:
    # Payload larger than MAX_PAYLOAD_SIZE
    large_payload = b"a" * (MAX_PAYLOAD_SIZE + 10)
    console_component.ctx.send_frame.return_value = True  # type: ignore[reportAttributeAccessIssue]

    await console_component._handle_mqtt_input(  # type: ignore[reportPrivateUsage]
        large_payload
    )  # pyright: ignore[reportPrivateUsage]

    assert console_component.ctx.send_frame.await_count >= 2  # type: ignore[reportUnknownMemberType]


@pytest.mark.asyncio
async def test_flush_queue(console_component: ConsoleComponent) -> None:
    # Setup mock state behavior
    queue = deque([b"queued"])
    console_component.state.console_to_mcu_queue = queue  # type: ignore[reportAttributeAccessIssue]
    console_component.state.pop_console_chunk.side_effect = (  # type: ignore[reportAttributeAccessIssue]
        lambda: (queue.popleft() if queue else None)
    )

    console_component.ctx.send_frame.return_value = True  # type: ignore[reportAttributeAccessIssue]

    await console_component.flush_queue()

    from mcubridge.protocol import structures

    expected = structures.ConsoleWritePacket(data=b"queued").encode()
    console_component.ctx.send_frame.assert_awaited_once_with(  # type: ignore[reportUnknownMemberType]
        Command.CMD_CONSOLE_WRITE.value,
        expected,
    )
    assert len(queue) == 0
