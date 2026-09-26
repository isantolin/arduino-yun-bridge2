from __future__ import annotations

import asyncio
import time
from collections.abc import Awaitable, Callable, Iterator
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock

import pytest
import tenacity
from hypothesis import given
from hypothesis import strategies as st
from mcubridge.config.settings import RuntimeConfig
from mcubridge.protocol import mcubridge_pb2 as pb
from mcubridge.protocol.protocol import Command, Status
from mcubridge.services.handshake import (
    HandshakeEvent,
    HandshakeState,
    SerialHandshakeManager,
    derive_serial_timing,
)
from mcubridge.state.context import RuntimeState, create_runtime_state


@pytest.fixture
def handshake_setup(
    tmp_path: Path,
) -> Iterator[tuple[SerialHandshakeManager, RuntimeState, AsyncMock, RuntimeConfig, pb.HandshakeConfig, AsyncMock]]:
    config = RuntimeConfig(
        topic_prefix="br",
        serial_port="/dev/test",
        serial_shared_secret=b"secure_secret_123456789012345678",
        serial_handshake_fatal_failures=3,
        file_system_root=str(tmp_path),
        allow_non_tmp_paths=True,
    )
    state = create_runtime_state(config)

    send_frame = AsyncMock(return_value=True)
    enqueue_cloud = AsyncMock()
    acknowledge_frame = AsyncMock()
    timing = derive_serial_timing(config)

    manager = SerialHandshakeManager(
        config=config,
        state=state,
        serial_timing=timing,
        send_frame=send_frame,
        enqueue_cloud=enqueue_cloud,
        acknowledge_frame=acknowledge_frame,
    )
    yield manager, state, send_frame, config, timing, acknowledge_frame
    state.cleanup()


@pytest.mark.asyncio
@pytest.mark.timeout(10)
async def test_handshake_auth_mismatch(
    handshake_setup: tuple[
        SerialHandshakeManager, RuntimeState, AsyncMock, RuntimeConfig, pb.HandshakeConfig, AsyncMock
    ],
) -> None:
    """Verify rejection of invalid HMAC tags during sync."""
    manager, state, _, _config, _timing, _ack = handshake_setup

    # Start sync to set expectations — MUST be cancelled to avoid dangling
    # tenacity retries (up to 3 × 5s timeout) that hang xdist workers.
    sync_task = asyncio.create_task(manager.synchronize())
    await asyncio.sleep(0.2)  # Let it send RESET and SYNC

    nonce = state.link_handshake_nonce
    assert nonce is not None

    # Simulate MCU response with WRONG tag
    bad_tag = b"F" * 16
    payload = pb.LinkSync(nonce=nonce, tag=bad_tag).SerializeToString()

    result = await manager.handle_link_sync_resp(1, payload)
    assert not result
    assert state.handshake_failure_streak == 1
    assert state.last_handshake_error == "sync_auth_mismatch"

    sync_task.cancel()
    with pytest.raises((asyncio.CancelledError, tenacity.RetryError)):
        await sync_task


@pytest.mark.asyncio
async def test_handshake_rate_limiting(
    handshake_setup: tuple[
        SerialHandshakeManager, RuntimeState, AsyncMock, RuntimeConfig, pb.HandshakeConfig, AsyncMock
    ],
) -> None:
    """Verify handshake rate limiting protects MCU from thrashing."""
    manager, state, _, config, _timing, _ack = handshake_setup
    config.serial_handshake_min_interval = 1.0

    state.connection_fsm.synchronize()
    state.link_handshake_nonce = b"pending"
    state.handshake_rate_until = time.monotonic() + 0.5

    # Try to process response while rate limited
    result = await manager.handle_link_sync_resp(1, b"")
    assert not result
    assert state.last_handshake_error == "sync_rate_limited"


@pytest.mark.asyncio
async def test_handshake_fatal_threshold(
    handshake_setup: tuple[
        SerialHandshakeManager, RuntimeState, AsyncMock, RuntimeConfig, pb.HandshakeConfig, AsyncMock
    ],
) -> None:
    """Verify transition to permanent failure after threshold is reached."""
    manager, state, _, _config, _timing, _ack = handshake_setup

    # sync_auth_mismatch is an immediate fatal reason, so it increments every time
    for _ in range(3):
        await manager.handle_handshake_failure("sync_auth_mismatch")

    assert state.handshake_fatal_count == 3
    assert state.handshake_fatal_reason is not None


@pytest.mark.asyncio
async def test_handshake_streak_fatal_threshold(
    handshake_setup: tuple[
        SerialHandshakeManager, RuntimeState, AsyncMock, RuntimeConfig, pb.HandshakeConfig, AsyncMock
    ],
) -> None:
    """Verify transition to permanent failure after streak threshold is reached for non-immediate errors."""
    manager, state, _, _config, _timing, _ack = handshake_setup

    # non-immediate reason
    reason = "link_reset_send_failed"
    for _ in range(2):
        await manager.handle_handshake_failure(reason)
        assert state.handshake_fatal_count == 0

    # 3rd failure hits the threshold (3)
    await manager.handle_handshake_failure(reason)
    assert state.handshake_fatal_count == 1


@pytest.mark.asyncio
@pytest.mark.timeout(10)
async def test_handshake_capabilities_retry(
    handshake_setup: tuple[
        SerialHandshakeManager, RuntimeState, AsyncMock, RuntimeConfig, pb.HandshakeConfig, AsyncMock
    ],
) -> None:
    """Verify capabilities discovery retries on timeout then succeeds with valid payload."""
    manager, _state, send_frame, _config, timing, _ack = handshake_setup

    timing.response_timeout_ms = 10

    attempts = 0

    async def mock_send_frame(cmd: int, *args: Any, **kwargs: Any) -> bool:
        nonlocal attempts
        attempts += 1
        fut: Any = getattr(manager, "_capabilities_future", None)
        if attempts < 3:
            if fut is not None and not fut.done():
                fut.set_exception(TimeoutError("mock timeout"))
        elif attempts == 3:
            # [SIL-2] Use valid empty Capabilities payload (serialized empty protobuf)
            asyncio.create_task(manager.handle_capabilities_resp(1, pb.Capabilities().SerializeToString()))
        return True

    send_frame.side_effect = mock_send_frame

    result = await getattr(manager, "_fetch_capabilities")()
    assert result
    assert send_frame.call_count == 3


@pytest.mark.asyncio
@pytest.mark.timeout(10)
async def test_handshake_capabilities_corrupt_payload(
    handshake_setup: tuple[
        SerialHandshakeManager, RuntimeState, AsyncMock, RuntimeConfig, pb.HandshakeConfig, AsyncMock
    ],
) -> None:
    """Verify capabilities discovery returns False immediately on corrupt payload. [SIL-2]"""
    manager, _state, send_frame, _config, timing, _ack = handshake_setup

    timing.response_timeout_ms = 200

    async def mock_send_frame_corrupt(cmd: int, *args: Any, **kwargs: Any) -> bool:
        fut: Any = getattr(manager, "_capabilities_future", None)
        if fut is not None and not fut.done():
            asyncio.create_task(manager.handle_capabilities_resp(1, b"\x80"))
        return True

    send_frame.side_effect = mock_send_frame_corrupt

    result = await getattr(manager, "_fetch_capabilities")()
    assert not result


@pytest.mark.asyncio
@pytest.mark.timeout(10)
async def test_handshake_malformed_sync_resp(
    handshake_setup: tuple[
        SerialHandshakeManager, RuntimeState, AsyncMock, RuntimeConfig, pb.HandshakeConfig, AsyncMock
    ],
) -> None:
    """Verify handling of corrupt protobuf in sync response."""
    manager, state, _, _config, _timing, _ack = handshake_setup
    state.link_handshake_nonce = b"pending"

    result = await manager.handle_link_sync_resp(1, b"\xff\xff\xff")  # Invalid protobuf
    assert not result
    assert state.last_handshake_error == "sync_decode_failed"
    handshake_setup[-1].assert_called_with(Command.CMD_LINK_SYNC_RESP.value, 1, status=Status.MALFORMED)


@pytest.mark.asyncio
async def test_wait_for_link_sync_confirmation_timeout(
    handshake_setup: tuple[
        SerialHandshakeManager, RuntimeState, AsyncMock, RuntimeConfig, pb.HandshakeConfig, AsyncMock
    ],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    manager, state, _, _config, _timing, _ack = handshake_setup
    _timing.response_timeout_ms = 10
    state.connection_fsm.disconnect()
    real_timeout = asyncio.timeout

    def _mock_timeout(_delay: float | None) -> asyncio.Timeout:
        return real_timeout(0.001)

    monkeypatch.setattr("asyncio.timeout", _mock_timeout)
    wait_sync: Callable[[bytes], Awaitable[bool]] = getattr(manager, "_wait_for_link_sync_confirmation")
    res = await wait_sync(b"test_nonce")
    assert res is False


@pytest.mark.asyncio
async def test_handshake_attempt_link_sync_timeout(
    handshake_setup: tuple[
        SerialHandshakeManager, RuntimeState, AsyncMock, RuntimeConfig, pb.HandshakeConfig, AsyncMock
    ],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    manager, _state, _, _config, _timing, _ack = handshake_setup
    monkeypatch.setattr(manager, "_wait_for_link_sync_confirmation", AsyncMock(return_value=False))
    mock_fail = AsyncMock()
    monkeypatch.setattr(manager, "handle_handshake_failure", mock_fail)

    sync_attempt: Callable[[], Awaitable[bool]] = getattr(manager, "_synchronize_attempt")
    res = await sync_attempt()
    assert res is False
    assert mock_fail.called


def test_handshake_calculate_tag_empty_secret() -> None:
    assert SerialHandshakeManager.calculate_handshake_tag(None, b"test_nonce") == b""
    assert SerialHandshakeManager.calculate_handshake_tag(b"", b"test_nonce") == b""


@given(secret=st.binary(min_size=16, max_size=32), nonce=st.binary(min_size=12, max_size=12))
def test_handshake_calculate_tag_deterministic_property(secret: bytes, nonce: bytes) -> None:
    tag1 = SerialHandshakeManager.calculate_handshake_tag(secret, nonce)
    tag2 = SerialHandshakeManager.calculate_handshake_tag(secret, nonce)
    assert tag1 == tag2
    assert len(tag1) == 16


@pytest.mark.asyncio
async def test_handshake_wait_confirmation_already_synchronized(
    runtime_config: RuntimeConfig, runtime_state: RuntimeState
) -> None:
    hs = SerialHandshakeManager(
        config=runtime_config,
        state=runtime_state,
        serial_timing=pb.HandshakeConfig(),
        send_frame=AsyncMock(return_value=True),
        enqueue_cloud=AsyncMock(),
        acknowledge_frame=AsyncMock(),
    )
    runtime_state.connection_fsm.synchronize()

    wait_sync: Callable[[bytes], Awaitable[bool]] = getattr(hs, "_wait_for_link_sync_confirmation")
    confirmed = await wait_sync(b"nonce")
    assert confirmed is True


@pytest.mark.asyncio
async def test_handshake_sync_state_permutations(
    runtime_config: RuntimeConfig, runtime_state: RuntimeState, monkeypatch: pytest.MonkeyPatch
) -> None:
    runtime_state.connection_fsm.disconnect()
    mock_send = AsyncMock(return_value=True)
    hs = SerialHandshakeManager(
        config=runtime_config,
        state=runtime_state,
        serial_timing=pb.HandshakeConfig(),
        send_frame=mock_send,
        enqueue_cloud=AsyncMock(),
        acknowledge_frame=AsyncMock(),
    )
    sync_attempt: Callable[[], Awaitable[bool]] = getattr(hs, "_synchronize_attempt")

    async def _send_and_fault(cmd: int, payload: Any) -> bool:
        if cmd == Command.CMD_LINK_SYNC.value:
            hs.fsm_state = HandshakeState.FAULT
        return True

    monkeypatch.setattr(hs, "_send_frame", _send_and_fault)
    assert await sync_attempt() is False

    hs.fsm_state = HandshakeState.SYNCING

    async def _mock_wait_fault(nonce: bytes) -> bool:
        hs.fsm_state = HandshakeState.FAULT
        return False

    setattr(hs, "_wait_for_link_sync_confirmation", _mock_wait_fault)
    assert await sync_attempt() is False

    monkeypatch.setattr(hs, "_wait_for_link_sync_confirmation", AsyncMock(return_value=False))
    hs.fsm_state = HandshakeState.SYNCING
    runtime_state.link_handshake_nonce = b"different_nonce"
    assert await sync_attempt() is False

    hs.transition(HandshakeEvent.RESET)
    monkeypatch.setattr(hs, "_send_frame", AsyncMock(return_value=True))
    monkeypatch.setattr(hs, "_wait_for_link_sync_confirmation", AsyncMock(return_value=True))
    assert await sync_attempt() is True


@pytest.mark.asyncio
async def test_handshake_resp_rate_limit_and_secret_none(
    runtime_config: RuntimeConfig, runtime_state: RuntimeState, monkeypatch: pytest.MonkeyPatch
) -> None:
    runtime_config.serial_handshake_min_interval = 5.0
    hs = SerialHandshakeManager(
        config=runtime_config,
        state=runtime_state,
        serial_timing=pb.HandshakeConfig(),
        send_frame=AsyncMock(return_value=True),
        enqueue_cloud=AsyncMock(),
        acknowledge_frame=AsyncMock(),
    )

    runtime_state.link_handshake_nonce = b"expected_nonce_12b"
    runtime_state.handshake_rate_until = time.monotonic() + 10.0
    pkt = pb.LinkSync(nonce=b"expected_nonce_12b", tag=b"tag")
    assert await hs.handle_link_sync_resp(1, pkt) is False

    runtime_config.serial_handshake_min_interval = 0.0
    runtime_config.serial_shared_secret = b""
    runtime_state.handshake_rate_until = 0.0
    nonce = b"expected_12b_nonce"
    runtime_state.link_handshake_nonce = nonce
    tag = hs.calculate_handshake_tag(b"", nonce)
    runtime_state.link_expected_tag = tag
    pkt_matching = pb.LinkSync(nonce=nonce, tag=tag)
    monkeypatch.setattr(hs, "_handle_handshake_success", AsyncMock())
    monkeypatch.setattr(hs, "_fetch_capabilities_with_delay", AsyncMock())
    assert await hs.handle_link_sync_resp(2, pkt_matching) is True
    assert runtime_state.link_session_key is None


@pytest.mark.asyncio
async def test_handshake_capabilities_resp_future_none(
    runtime_config: RuntimeConfig, runtime_state: RuntimeState
) -> None:
    hs = SerialHandshakeManager(
        config=runtime_config,
        state=runtime_state,
        serial_timing=pb.HandshakeConfig(),
        send_frame=AsyncMock(return_value=True),
        enqueue_cloud=AsyncMock(),
        acknowledge_frame=AsyncMock(),
    )
    setattr(hs, "_capabilities_future", None)
    assert await hs.handle_capabilities_resp(1, b"") is True


@pytest.mark.asyncio
async def test_handshake_fsm_state_override_and_unexpected_resp(
    runtime_config: RuntimeConfig, runtime_state: RuntimeState, monkeypatch: pytest.MonkeyPatch
) -> None:
    hs = SerialHandshakeManager(
        config=runtime_config,
        state=runtime_state,
        serial_timing=pb.HandshakeConfig(),
        send_frame=AsyncMock(return_value=True),
        enqueue_cloud=AsyncMock(),
        acknowledge_frame=AsyncMock(),
    )

    set_fsm_state: Callable[[HandshakeState], None] = getattr(hs, "_set_fsm_state")
    set_fsm_state(HandshakeState.SYNCHRONIZED)
    assert hs.fsm_state == HandshakeState.SYNCHRONIZED
    set_fsm_state(HandshakeState.SYNCHRONIZED)  # no-op same state
    set_fsm_state(HandshakeState.UNSYNCHRONIZED)
    assert hs.fsm_state == HandshakeState.UNSYNCHRONIZED

    runtime_state.link_handshake_nonce = None
    mock_fail = AsyncMock()
    monkeypatch.setattr(hs, "handle_handshake_failure", mock_fail)
    res = await hs.handle_link_sync_resp(1, pb.LinkSync(nonce=b"none", tag=b"tag"))
    assert res is False
    assert mock_fail.called

    publish_event: Callable[..., Awaitable[None]] = getattr(hs, "_publish_handshake_event")

    def mock_get_topic(*_a: Any, **_k: Any) -> str:
        return ""

    monkeypatch.setattr("mcubridge.services.handshake.get_topic_for_message", mock_get_topic)
    await publish_event("test_event", reason="test_err")
    assert getattr(hs, "_enqueue_cloud").await_count == 0


@pytest.mark.asyncio
async def test_handshake_rate_limit_and_sync_fault_branches(
    runtime_config: RuntimeConfig,
    runtime_state: RuntimeState,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from mcubridge.services.handshake import RateLimiter

    hs = SerialHandshakeManager(
        config=runtime_config,
        state=runtime_state,
        serial_timing=pb.HandshakeConfig(),
        send_frame=AsyncMock(return_value=True),
        enqueue_cloud=AsyncMock(),
        acknowledge_frame=AsyncMock(),
    )

    # 1. check_rate_limit when now >= limit_until (line 121)
    allowed, remaining = RateLimiter.check_rate_limit(10.0, 5.0)
    assert allowed is True
    assert remaining == 0.0

    # 2. _synchronize_attempt() when confirmed is False and current_state == HandshakeState.FAULT (line 328)
    monkeypatch.setattr(hs, "_wait_for_link_sync_confirmation", AsyncMock(return_value=False))
    set_fsm_state: Callable[[HandshakeState], None] = getattr(hs, "_set_fsm_state")
    set_fsm_state(HandshakeState.FAULT)
    sync_fn: Callable[[], Awaitable[bool]] = getattr(hs, "_synchronize_attempt")
    res_fault = await sync_fn()
    assert res_fault is False

    # 3. _synchronize_attempt() when pending_nonce != nonce (line 332->334)
    set_fsm_state(HandshakeState.UNSYNCHRONIZED)
    runtime_state.link_handshake_nonce = b"different_nonce_1234"
    res_mismatch = await sync_fn()
    assert res_mismatch is False
