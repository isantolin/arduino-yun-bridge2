# pyright: reportPrivateUsage=false
"""Surgical coverage tests for SerialHandshakeManager in services/handshake.py. [SIL-2]"""

from __future__ import annotations

import asyncio
from collections.abc import Iterator
from pathlib import Path
from unittest.mock import AsyncMock, patch

import pytest

from mcubridge.config.settings import RuntimeConfig
from mcubridge.protocol import mcubridge_pb2 as pb
from mcubridge.services.handshake import (
    HandshakeEvent,
    HandshakeMachine,
    HandshakeState,
    RateLimiter,
    SerialHandshakeManager,
    derive_serial_timing,
)
from mcubridge.state.context import RuntimeState, create_runtime_state


@pytest.fixture
def handshake_mgr(tmp_path: Path) -> Iterator[tuple[SerialHandshakeManager, RuntimeState, AsyncMock, AsyncMock]]:
    config = RuntimeConfig(
        topic_prefix="br",
        serial_port="/dev/test",
        serial_shared_secret=b"test_secret_1234567890",
        serial_handshake_fatal_failures=3,
        file_system_root=str(tmp_path),
        allow_non_tmp_paths=True,
    )
    state = create_runtime_state(config)
    send_frame = AsyncMock(return_value=True)
    enqueue_cloud = AsyncMock()
    ack_frame = AsyncMock()

    timing = derive_serial_timing(config)
    mgr = SerialHandshakeManager(
        config=config,
        state=state,
        serial_timing=timing,
        send_frame=send_frame,
        enqueue_cloud=enqueue_cloud,
        acknowledge_frame=ack_frame,
    )
    try:
        yield mgr, state, send_frame, enqueue_cloud
    finally:
        state.cleanup()


@pytest.mark.asyncio
async def test_synchronize_send_reset_failed(
    handshake_mgr: tuple[SerialHandshakeManager, RuntimeState, AsyncMock, AsyncMock],
) -> None:
    mgr, _state, send_frame, _enqueue = handshake_mgr
    send_frame.return_value = False
    result = await mgr.synchronize()
    assert result is False
    assert mgr.fsm_state == HandshakeState.FAULT


@pytest.mark.asyncio
async def test_synchronize_send_sync_failed(
    handshake_mgr: tuple[SerialHandshakeManager, RuntimeState, AsyncMock, AsyncMock],
) -> None:
    mgr, _state, send_frame, _enqueue = handshake_mgr
    # Send RESET (True) then Send SYNC (False) repeatedly
    send_frame.side_effect = [True, False, False, False, False, False, False, False]
    result = await mgr.synchronize()
    assert result is False
    assert mgr.fsm_state == HandshakeState.FAULT


@pytest.mark.asyncio
async def test_handle_link_sync_resp_without_pending_nonce(
    handshake_mgr: tuple[SerialHandshakeManager, RuntimeState, AsyncMock, AsyncMock],
) -> None:
    mgr, state, _send, _enqueue = handshake_mgr
    state.link_handshake_nonce = None
    result = await mgr.handle_link_sync_resp(1, b"")
    assert result is False
    assert state.last_handshake_error == "unexpected_sync_resp"


@pytest.mark.asyncio
async def test_handle_capabilities_resp(
    handshake_mgr: tuple[SerialHandshakeManager, RuntimeState, AsyncMock, AsyncMock],
) -> None:
    mgr, state, _send, _enqueue = handshake_mgr
    loop = asyncio.get_event_loop()
    mgr._capabilities_future = loop.create_future()
    cap = pb.Capabilities()
    result = await mgr.handle_capabilities_resp(1, cap)
    assert result is True
    assert mgr._capabilities_future.result() == cap

    mgr._parse_capabilities(cap)
    assert state.mcu_capabilities == cap


@pytest.mark.asyncio
async def test_handle_link_reset_resp(
    handshake_mgr: tuple[SerialHandshakeManager, RuntimeState, AsyncMock, AsyncMock],
) -> None:
    mgr, _state, _send, _enqueue = handshake_mgr
    result = await mgr.handle_link_reset_resp(1, b"reset_ack")
    assert result is True


@pytest.mark.asyncio
async def test_calculate_session_key_and_tag() -> None:
    secret = b"test_secret_32bytes_long_secret!"
    nonce = b"123456789012"
    tag = SerialHandshakeManager.calculate_handshake_tag(secret, nonce)
    assert len(tag) == 16

    key = SerialHandshakeManager.calculate_session_key(secret, nonce)
    assert len(key) == 32


@pytest.mark.asyncio
async def test_handle_link_sync_resp_decode_and_auth_failures(
    handshake_mgr: tuple[SerialHandshakeManager, RuntimeState, AsyncMock, AsyncMock],
) -> None:
    mgr, state, _send, _enqueue = handshake_mgr

    # 1. Corrupt payload decode error
    state.link_handshake_nonce = b"1234567890123456"
    res1 = await mgr.handle_link_sync_resp(1, b"invalid-protobuf-garbage-\xff\xff")
    assert res1 is False
    assert state.last_handshake_error == "sync_decode_failed"

    # 2. Auth mismatch (incorrect tag)
    state.link_handshake_nonce = b"1234567890123456"
    state.link_expected_tag = b"correct_expected_tag"
    sync_pkt = pb.LinkSync(nonce=b"1234567890123456", tag=b"wrong_tag_value")
    res2 = await mgr.handle_link_sync_resp(2, sync_pkt)
    assert res2 is False
    assert state.last_handshake_error == "sync_auth_mismatch"


@pytest.mark.asyncio
async def test_fetch_capabilities_failure_paths(
    handshake_mgr: tuple[SerialHandshakeManager, RuntimeState, AsyncMock, AsyncMock],
) -> None:
    mgr, _state, send_frame, _enqueue = handshake_mgr
    send_frame.return_value = False

    def _zero_wait(_rs: object) -> float:
        return 0.0

    with patch("tenacity.wait_exponential", return_value=_zero_wait):
        res = await mgr._fetch_capabilities()
        assert res is False


def test_rate_limiter_calculations() -> None:
    # Check rate limit allowed
    allowed, rem = RateLimiter.check_rate_limit(10.0, 5.0)
    assert allowed is True
    assert rem == 0.0

    # Check rate limit throttled
    allowed, rem = RateLimiter.check_rate_limit(5.0, 10.0)
    assert allowed is False
    assert rem == 5.0

    # Exponential backoff calculations
    assert RateLimiter.compute_exponential_backoff(0, base=1.0, max_delay=10.0) == 1.0
    assert RateLimiter.compute_exponential_backoff(1, base=1.0, max_delay=10.0) == 2.0
    assert RateLimiter.compute_exponential_backoff(2, base=1.0, max_delay=10.0) == 4.0
    assert RateLimiter.compute_exponential_backoff(3, base=1.0, max_delay=10.0) == 8.0
    assert RateLimiter.compute_exponential_backoff(4, base=1.0, max_delay=10.0) == 10.0
    assert RateLimiter.compute_exponential_backoff(-1, base=1.0, max_delay=10.0) == 1.0


def test_handshake_machine_transitions() -> None:
    m = HandshakeMachine()
    assert m.current_state_value == HandshakeState.UNSYNCHRONIZED.value

    # Test complete happy cycle
    m.start_sync()
    assert m.current_state_value == HandshakeState.RESETTING.value
    m.reset_sent()
    assert m.current_state_value == HandshakeState.SYNCING.value
    m.sync_sent()
    assert m.current_state_value == HandshakeState.CONFIRMING.value
    m.sync_confirmed()
    assert m.current_state_value == HandshakeState.SYNCHRONIZED.value

    # Reset back to unsynchronized
    m.reset()
    assert m.current_state_value == HandshakeState.UNSYNCHRONIZED.value

    # Direct fast-emulator transition: syncing -> synchronized
    m.start_sync()
    m.reset_sent()
    m.sync_confirmed()
    assert m.current_state_value == HandshakeState.SYNCHRONIZED.value

    # Failure from synchronized
    m.failure()
    assert m.current_state_value == HandshakeState.FAULT.value

    # Start sync from fault
    m.start_sync()
    assert m.current_state_value == HandshakeState.RESETTING.value
    m.failure()
    assert m.current_state_value == HandshakeState.FAULT.value


def test_handshake_manager_transition_dispatch(
    handshake_mgr: tuple[SerialHandshakeManager, RuntimeState, AsyncMock, AsyncMock],
) -> None:
    mgr, state, _send, _enqueue = handshake_mgr
    assert mgr.fsm_state == HandshakeState.UNSYNCHRONIZED

    # Valid progression
    s1 = mgr.transition(HandshakeEvent.START_SYNC)
    assert s1 == HandshakeState.RESETTING
    assert mgr.fsm_state == HandshakeState.RESETTING

    s2 = mgr.transition(HandshakeEvent.RESET_SENT)
    assert s2 == HandshakeState.SYNCING

    s3 = mgr.transition(HandshakeEvent.SYNC_SENT)
    assert s3 == HandshakeState.CONFIRMING

    s4 = mgr.transition(HandshakeEvent.SYNC_CONFIRMED)
    assert s4 == HandshakeState.SYNCHRONIZED
    assert state.is_synchronized

    # Transitioning away from SYNCHRONIZED
    s5 = mgr.transition(HandshakeEvent.RESET)
    assert s5 == HandshakeState.UNSYNCHRONIZED
    assert not state.is_synchronized

    # Invalid transition (rejected and state unchanged)
    s_invalid = mgr.transition(HandshakeEvent.SYNC_CONFIRMED)
    assert s_invalid == HandshakeState.UNSYNCHRONIZED
    assert mgr.fsm_state == HandshakeState.UNSYNCHRONIZED

    # Failure transition
    s_fail = mgr.transition(HandshakeEvent.FAILURE)
    assert s_fail == HandshakeState.FAULT

    # Explicit setter
    mgr.fsm_state = HandshakeState.RESETTING
    assert mgr.fsm_state == HandshakeState.RESETTING
    # Idempotent setter
    mgr.fsm_state = HandshakeState.RESETTING
    assert mgr.fsm_state == HandshakeState.RESETTING
