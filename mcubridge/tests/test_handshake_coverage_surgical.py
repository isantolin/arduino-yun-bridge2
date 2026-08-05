"""Surgical coverage tests for SerialHandshakeManager in services/handshake.py. [SIL-2]"""

from __future__ import annotations

import asyncio
import os
import time
from collections.abc import Iterator
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest

from mcubridge.config.settings import RuntimeConfig
from mcubridge.protocol import mcubridge_pb2 as pb
from mcubridge.protocol.protocol import Command, Status
from mcubridge.services.handshake import HandshakeState, SerialHandshakeManager, derive_serial_timing
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
    mgr, state, send_frame, _enqueue = handshake_mgr
    send_frame.return_value = False  # Reset frame send fails
    result = await mgr.synchronize()
    assert result is False
    assert state.fsm_state == HandshakeState.FAULT


@pytest.mark.asyncio
async def test_synchronize_send_sync_failed(
    handshake_mgr: tuple[SerialHandshakeManager, RuntimeState, AsyncMock, AsyncMock],
) -> None:
    mgr, state, send_frame, _enqueue = handshake_mgr
    # First send_frame (RESET) succeeds, second (SYNC) fails
    send_frame.side_effect = [True, False]
    result = await mgr.synchronize()
    assert result is False
    assert state.fsm_state == HandshakeState.FAULT


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
    cap = pb.CapabilitiesResponse(protocol_version=2)
    result = await mgr.handle_capabilities_resp(1, cap)
    assert result is True


@pytest.mark.asyncio
async def test_handle_capabilities_resp_invalid_payload(
    handshake_mgr: tuple[SerialHandshakeManager, RuntimeState, AsyncMock, AsyncMock],
) -> None:
    mgr, state, _send, _enqueue = handshake_mgr
    result = await mgr.handle_capabilities_resp(1, b"\xff\xff\xff")
    assert result is False


@pytest.mark.asyncio
async def test_handle_link_reset_resp(
    handshake_mgr: tuple[SerialHandshakeManager, RuntimeState, AsyncMock, AsyncMock],
) -> None:
    mgr, state, _send, _enqueue = handshake_mgr
    reset_msg = pb.LinkReset(reason="test_reset")
    result = await mgr.handle_link_reset_resp(1, reset_msg)
    assert result is True


@pytest.mark.asyncio
async def test_calculate_session_key_and_tag() -> None:
    secret = b"test_secret_32bytes_long_secret!"
    nonce = b"123456789012"
    tag = SerialHandshakeManager.calculate_handshake_tag(secret, nonce)
    assert len(tag) == 16

    empty_tag = SerialHandshakeManager.calculate_handshake_tag(None, nonce)
    assert empty_tag == b""

    key = SerialHandshakeManager.calculate_session_key(secret, nonce)
    assert len(key) == 32
