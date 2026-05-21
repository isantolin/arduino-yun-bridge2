import asyncio
import time
from typing import cast
from unittest.mock import AsyncMock, patch

import pytest
import msgspec

# pyright: reportPrivateUsage=false
from mcubridge.services.handshake import SerialHandshakeManager, SerialHandshakeFatal
from mcubridge.config.settings import RuntimeConfig
from mcubridge.state.context import create_runtime_state, RuntimeState
from mcubridge.protocol.protocol import Command, Status
from mcubridge.protocol.structures import LinkSyncPacket


@pytest.fixture
def handshake_setup() -> tuple[SerialHandshakeManager, RuntimeState, AsyncMock]:
    config = RuntimeConfig(
        mqtt_topic="br",
        serial_port="/dev/test",
        serial_shared_secret=b"secure_secret_123456789012345678",
        serial_handshake_fatal_failures=3,
    )
    state = create_runtime_state(config)

    send_frame = AsyncMock(return_value=True)
    enqueue_mqtt = AsyncMock()
    acknowledge_frame = AsyncMock()

    from mcubridge.services.handshake import derive_serial_timing

    timing = derive_serial_timing(config)

    manager = SerialHandshakeManager(
        config=config,
        state=state,
        serial_timing=timing,
        send_frame=send_frame,
        enqueue_mqtt=enqueue_mqtt,
        acknowledge_frame=acknowledge_frame,
    )
    return manager, state, send_frame


@pytest.mark.asyncio
async def test_handshake_auth_mismatch(
    handshake_setup: tuple[SerialHandshakeManager, RuntimeState, AsyncMock],
) -> None:
    """Verify rejection of invalid HMAC tags during sync."""
    manager, state, _ = handshake_setup

    # Start sync to set expectations
    asyncio.create_task(manager.synchronize())
    await asyncio.sleep(0.2)  # Let it send RESET and SYNC

    nonce = state.link_handshake_nonce
    assert nonce is not None

    # Simulate MCU response with WRONG tag
    bad_tag = b"F" * 16
    payload = LinkSyncPacket(nonce=nonce, tag=bad_tag).encode()

    result = await manager.handle_link_sync_resp(1, payload)
    assert result is False
    assert state.handshake_failure_streak == 1
    assert state.last_handshake_error == "sync_auth_mismatch"


@pytest.mark.asyncio
async def test_handshake_rate_limiting(
    handshake_setup: tuple[SerialHandshakeManager, RuntimeState, AsyncMock],
) -> None:
    """Verify handshake rate limiting protects MCU from thrashing."""
    manager, state, _ = handshake_setup
    manager._config = msgspec.structs.replace(manager._config, serial_handshake_min_interval=1.0)

    state.mark_synchronized()
    state.link_handshake_nonce = b"pending"
    state.handshake_rate_until = time.monotonic() + 0.5

    # Try to process response while rate limited
    result = await manager.handle_link_sync_resp(1, b"")
    assert result is False
    assert state.last_handshake_error == "sync_rate_limited"


@pytest.mark.asyncio
async def test_handshake_fatal_threshold(
    handshake_setup: tuple[SerialHandshakeManager, RuntimeState, AsyncMock],
) -> None:
    """Verify transition to permanent failure after threshold is reached."""
    manager, state, _ = handshake_setup

    # sync_auth_mismatch is an immediate fatal reason, so it increments every time
    for _ in range(3):
        await manager.handle_handshake_failure("sync_auth_mismatch")

    assert state.handshake_fatal_count == 3
    with pytest.raises(SerialHandshakeFatal):
        manager.raise_if_handshake_fatal()


@pytest.mark.asyncio
async def test_handshake_streak_fatal_threshold(
    handshake_setup: tuple[SerialHandshakeManager, RuntimeState, AsyncMock],
) -> None:
    """Verify transition to permanent failure after streak threshold is reached for non-immediate errors."""
    manager, state, _ = handshake_setup

    # non-immediate reason
    reason = "link_reset_send_failed"
    for _ in range(2):
        await manager.handle_handshake_failure(reason)
        assert state.handshake_fatal_count == 0

    # 3rd failure hits the threshold (3)
    await manager.handle_handshake_failure(reason)
    assert state.handshake_fatal_count == 1


@pytest.mark.asyncio
async def test_handshake_capabilities_retry(
    handshake_setup: tuple[SerialHandshakeManager, RuntimeState, AsyncMock],
) -> None:
    """Verify capabilities discovery retries on timeout."""
    manager, _, send_frame = handshake_setup

    # Simulate timeout on first 2 attempts, success on 3rd
    manager._timing = msgspec.structs.replace(manager._timing, response_timeout_ms=10)

    with patch("asyncio.wait_for") as mock_wait:
        mock_wait.side_effect = [
            asyncio.TimeoutError,
            asyncio.TimeoutError,
            b"\x80",
        ]  # Empty map

        result = await manager._fetch_capabilities()
        assert result is True
        assert send_frame.call_count == 3


@pytest.mark.asyncio
async def test_handshake_malformed_sync_resp(
    handshake_setup: tuple[SerialHandshakeManager, RuntimeState, AsyncMock],
) -> None:
    """Verify handling of corrupt MsgPack in sync response."""
    manager, state, _ = handshake_setup
    state.link_handshake_nonce = b"pending"

    result = await manager.handle_link_sync_resp(1, b"\xff\xff\xff")  # Invalid msgpack
    assert result is False
    assert state.last_handshake_error == "sync_decode_failed"
    cast(AsyncMock, manager._acknowledge_frame).assert_called_with(
        Command.CMD_LINK_SYNC_RESP.value, 1, status=Status.MALFORMED
    )
