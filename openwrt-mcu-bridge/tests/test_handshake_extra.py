"""Extra coverage for mcubridge.services.handshake."""

import asyncio
import time
from unittest.mock import ANY, AsyncMock, patch

import pytest
from mcubridge.config.settings import RuntimeConfig
from mcubridge.protocol.protocol import Command, Status
from mcubridge.services.handshake import (
    SerialHandshakeManager,
    derive_serial_timing,
)
from mcubridge.state.context import create_runtime_state


@pytest.mark.asyncio
async def test_handshake_link_reset_retry() -> None:
    """Test LINK_RESET retry without timing payload when first attempt fails."""
    config = RuntimeConfig(serial_shared_secret=b"secret_1234")
    state = create_runtime_state(config)
    timing = derive_serial_timing(config)
    # First call returns False, second returns True
    send_frame = AsyncMock(side_effect=[False, True, True])
    manager = SerialHandshakeManager(
        config=config, state=state, serial_timing=timing,
        send_frame=send_frame, enqueue_mqtt=AsyncMock(),
        acknowledge_frame=AsyncMock()
    )
    with patch("asyncio.sleep", return_value=None):
        await manager._synchronize_attempt()

    # Assert called at least twice for CMD_LINK_RESET
    assert send_frame.call_count >= 2
    assert send_frame.call_args_list[0][0][0] == Command.CMD_LINK_RESET.value
    assert send_frame.call_args_list[1][0][0] == Command.CMD_LINK_RESET.value
    assert send_frame.call_args_list[1][0][1] == b""


@pytest.mark.asyncio
async def test_handshake_sync_resp_rate_limit() -> None:
    """Test rate limiting in handle_link_sync_resp."""
    config = RuntimeConfig(serial_shared_secret=b"secret_1234", serial_handshake_min_interval=10.0)
    state = create_runtime_state(config)
    timing = derive_serial_timing(config)
    manager = SerialHandshakeManager(
        config=config, state=state, serial_timing=timing,
        send_frame=AsyncMock(), enqueue_mqtt=AsyncMock(),
        acknowledge_frame=AsyncMock()
    )
    state.link_handshake_nonce = b"A" * 16
    state.handshake_rate_limit_until = time.monotonic() + 5.0
    assert await manager.handle_link_sync_resp(b"A" * 32) is False
    manager._acknowledge_frame.assert_called_with(Command.CMD_LINK_SYNC_RESP.value, status=Status.MALFORMED, extra=ANY)


@pytest.mark.asyncio
async def test_handshake_sync_resp_replay_detected() -> None:
    """Test replay detection in handle_link_sync_resp."""
    config = RuntimeConfig(serial_shared_secret=b"secret_1234")
    state = create_runtime_state(config)
    timing = derive_serial_timing(config)
    manager = SerialHandshakeManager(
        config=config, state=state, serial_timing=timing,
        send_frame=AsyncMock(), enqueue_mqtt=AsyncMock(),
        acknowledge_frame=AsyncMock()
    )
    nonce = b"A" * 16
    state.link_handshake_nonce = nonce
    state.link_expected_tag = manager.compute_handshake_tag(nonce)

    # Mock validate_nonce_counter to fail (replay)
    with patch("mcubridge.services.handshake.validate_nonce_counter", return_value=(False, 0)):
        assert await manager.handle_link_sync_resp(nonce + state.link_expected_tag) is False


@pytest.mark.asyncio
async def test_handshake_fetch_capabilities_timeout_and_retry() -> None:
    """Test _fetch_capabilities retry logic on timeout."""
    config = RuntimeConfig(serial_shared_secret=b"secret_1234")
    state = create_runtime_state(config)
    timing = derive_serial_timing(config)
    # Succeeds on 3rd attempt
    send_frame = AsyncMock(return_value=True)
    manager = SerialHandshakeManager(
        config=config, state=state, serial_timing=timing,
        send_frame=send_frame, enqueue_mqtt=AsyncMock(),
        acknowledge_frame=AsyncMock()
    )

    with patch("asyncio.wait_for", side_effect=[asyncio.TimeoutError, asyncio.TimeoutError, b"data"]), \
         patch.object(manager, "_parse_capabilities"):
        assert await manager._fetch_capabilities() is True
        assert send_frame.call_count == 3


@pytest.mark.asyncio
async def test_handshake_handle_capabilities_resp() -> None:
    """Test handle_capabilities_resp completes the future."""
    config = RuntimeConfig(serial_shared_secret=b"secret_1234")
    state = create_runtime_state(config)
    timing = derive_serial_timing(config)
    manager = SerialHandshakeManager(
        config=config, state=state, serial_timing=timing,
        send_frame=AsyncMock(), enqueue_mqtt=AsyncMock(),
        acknowledge_frame=AsyncMock()
    )
    loop = asyncio.get_running_loop()
    manager._capabilities_future = loop.create_future()
    await manager.handle_capabilities_resp(b"payload")
    assert manager._capabilities_future.done()
    assert await manager._capabilities_future == b"payload"


@pytest.mark.asyncio
async def test_handshake_failure_detail_non_immediate() -> None:
    """Test handle_handshake_failure with streak-based fatal reason."""
    config = RuntimeConfig(serial_shared_secret=b"secret_1234", serial_handshake_fatal_failures=2)
    state = create_runtime_state(config)
    timing = derive_serial_timing(config)
    manager = SerialHandshakeManager(
        config=config, state=state, serial_timing=timing,
        send_frame=AsyncMock(), enqueue_mqtt=AsyncMock(),
        acknowledge_frame=AsyncMock()
    )

    await manager.handle_handshake_failure("timeout", detail="initial")
    assert state.handshake_fatal_count == 0

    # Second failure triggers fatal
    await manager.handle_handshake_failure("timeout")
    assert state.handshake_fatal_count == 1
    assert "streak" in state.handshake_fatal_detail


@pytest.mark.asyncio
async def test_handshake_clear_expectations_with_data() -> None:
    """Test clear_handshake_expectations zeroizes buffers."""
    config = RuntimeConfig(serial_shared_secret=b"secret_1234")
    state = create_runtime_state(config)
    timing = derive_serial_timing(config)
    manager = SerialHandshakeManager(
        config=config, state=state, serial_timing=timing,
        send_frame=AsyncMock(), enqueue_mqtt=AsyncMock(),
        acknowledge_frame=AsyncMock()
    )
    state.link_handshake_nonce = b"A" * 16
    state.link_expected_tag = b"B" * 16

    with patch("mcubridge.services.handshake.secure_zero") as mock_zero:
        manager.clear_handshake_expectations()
        assert mock_zero.call_count == 2
        assert state.link_handshake_nonce is None
        assert state.link_expected_tag is None
