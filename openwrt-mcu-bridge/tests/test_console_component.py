import pytest
import asyncio
from unittest.mock import MagicMock, AsyncMock, patch
from collections import deque

from mcubridge.protocol.protocol import Command, MAX_PAYLOAD_SIZE
from mcubridge.services.console import ConsoleComponent
from mcubridge.services.base import BridgeContext
from mcubridge.state.context import RuntimeState
from mcubridge.config.settings import RuntimeConfig


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
    await console_component.handle_write(payload)

    console_component.ctx.publish.assert_awaited_once()
    call_args = console_component.ctx.publish.call_args
    assert call_args.kwargs['topic'].endswith("console/out")
    assert call_args.kwargs['payload'] == payload


@pytest.mark.asyncio
async def test_flow_control(console_component: ConsoleComponent) -> None:
    # Initial state
    assert console_component.state.mcu_is_paused is False
    assert console_component.state.serial_tx_allowed.is_set() is True

    # XOFF
    await console_component.handle_xoff(b"")
    assert console_component.state.mcu_is_paused is True
    assert console_component.state.serial_tx_allowed.is_set() is False

    # XON
    with patch.object(console_component, "flush_queue", new_callable=AsyncMock) as mock_flush:
        await console_component.handle_xon(b"")
        assert console_component.state.mcu_is_paused is False
        assert console_component.state.serial_tx_allowed.is_set() is True
        mock_flush.assert_awaited_once()


@pytest.mark.asyncio
async def test_handle_mqtt_input_direct(console_component: ConsoleComponent) -> None:
    payload = b"input"
    console_component.ctx.send_frame.return_value = True

    await console_component.handle_mqtt_input(payload)

    console_component.ctx.send_frame.assert_awaited_once_with(Command.CMD_CONSOLE_WRITE.value, payload)


@pytest.mark.asyncio
async def test_handle_mqtt_input_paused(console_component: ConsoleComponent) -> None:
    console_component.state.mcu_is_paused = True
    payload = b"input"

    await console_component.handle_mqtt_input(payload)

    console_component.ctx.send_frame.assert_not_awaited()
    # verify enqueue was called since state is a mock
    # The actual queue won't change because enqueue_console_chunk is a mock
    from logging import getLogger
    logger = getLogger("mcubridge.console")
    # We can't easily match the logger instance exactly without patching, 
    # but we can check the payload.
    # Actually, let's just check call args.
    console_component.state.enqueue_console_chunk.assert_called_once()
    args = console_component.state.enqueue_console_chunk.call_args
    assert args[0][0] == payload


@pytest.mark.asyncio
async def test_handle_mqtt_input_chunking(console_component: ConsoleComponent) -> None:
    # Payload larger than MAX_PAYLOAD_SIZE
    large_payload = b"a" * (MAX_PAYLOAD_SIZE + 10)
    console_component.ctx.send_frame.return_value = True

    await console_component.handle_mqtt_input(large_payload)

    assert console_component.ctx.send_frame.await_count >= 2


@pytest.mark.asyncio
async def test_flush_queue(console_component: ConsoleComponent) -> None:
    # Setup mock state behavior
    queue = deque([b"queued"])
    console_component.state.console_to_mcu_queue = queue
    console_component.state.pop_console_chunk.side_effect = lambda: queue.popleft() if queue else None
    
    console_component.ctx.send_frame.return_value = True

    await console_component.flush_queue()

    console_component.ctx.send_frame.assert_awaited_once_with(Command.CMD_CONSOLE_WRITE.value, b"queued")
    assert len(queue) == 0
