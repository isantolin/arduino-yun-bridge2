from __future__ import annotations

from typing import Any, cast
from unittest.mock import AsyncMock, patch

import pytest
from mcubridge.services.handshake import SerialHandshakeManager, derive_serial_timing
from mcubridge.state.context import create_runtime_state
from mcubridge.protocol.protocol import Command, Status
from mcubridge.protocol.structures import LinkSyncPacket
import msgspec

@pytest.fixture
def handshake_mgr(runtime_config: Any):
    state = create_runtime_state(runtime_config)
    timing = derive_serial_timing(runtime_config)
    mgr = SerialHandshakeManager(
        config=runtime_config,
        state=state,
        serial_timing=timing,
        send_frame=AsyncMock(return_value=True),
        enqueue_mqtt=AsyncMock(),
        acknowledge_frame=AsyncMock()
    )
    return mgr

@pytest.mark.asyncio
async def test_handshake_trigger_all_states(handshake_mgr: SerialHandshakeManager):
    handshake_mgr.trigger("reset_fsm")
    assert handshake_mgr.fsm_state == "unsynchronized"

    handshake_mgr.trigger("start_reset")
    assert handshake_mgr.fsm_state == "resetting"

    handshake_mgr.trigger("start_sync")
    assert handshake_mgr.fsm_state == "syncing"

    handshake_mgr.trigger("start_confirm")
    assert handshake_mgr.fsm_state == "confirming"

    handshake_mgr.trigger("complete_handshake")
    assert handshake_mgr.fsm_state == "synchronized"

    handshake_mgr.trigger("reset_fsm")
    handshake_mgr.trigger("fail_handshake")
    assert handshake_mgr.fsm_state == "fault"

@pytest.mark.asyncio
async def test_handshake_synchronize_retry_exhaustion(handshake_mgr: SerialHandshakeManager):
    handshake_mgr._fatal_threshold = 2
    handshake_mgr._synchronize_attempt = AsyncMock(return_value=False)

    ok = await handshake_mgr.synchronize()
    assert ok is False
    assert handshake_mgr.fsm_state == "fault"
    assert handshake_mgr._synchronize_attempt.call_count == 2

@pytest.mark.asyncio
async def test_handshake_synchronize_success(handshake_mgr: SerialHandshakeManager):
    async def mock_attempt():
        # complete_handshake requires being in syncing or confirming state
        handshake_mgr.trigger("start_reset")
        handshake_mgr.trigger("start_sync")
        handshake_mgr.trigger("complete_handshake")
        return True
    
    handshake_mgr._synchronize_attempt = AsyncMock(side_effect=mock_attempt)
    ok = await handshake_mgr.synchronize()
    assert ok is True
    assert handshake_mgr.fsm_state == "synchronized"

@pytest.mark.asyncio
async def test_handshake_attempt_reset_send_fail(handshake_mgr: SerialHandshakeManager):
    # Mock send_frame to fail
    cast(AsyncMock, handshake_mgr._send_frame).return_value = False
    ok = await handshake_mgr._synchronize_attempt()
    assert ok is False

@pytest.mark.asyncio
async def test_handshake_attempt_sync_send_fail(handshake_mgr: SerialHandshakeManager):
    # First call (reset) succeeds, second (sync) fails
    cast(AsyncMock, handshake_mgr._send_frame).side_effect = [True, False]
    with patch("asyncio.sleep", return_value=None):
        ok = await handshake_mgr._synchronize_attempt()
    assert ok is False

@pytest.mark.asyncio
async def test_handle_link_sync_resp_no_nonce(handshake_mgr: SerialHandshakeManager):
    handshake_mgr._state.link_handshake_nonce = None
    ok = await handshake_mgr.handle_link_sync_resp(0, b"")
    assert ok is False
    cast(AsyncMock, handshake_mgr._acknowledge_frame).assert_called_with(
        Command.CMD_LINK_SYNC_RESP.value, 0, status=Status.MALFORMED
    )

@pytest.mark.asyncio
async def test_handle_link_sync_resp_decode_fail(handshake_mgr: SerialHandshakeManager):
    handshake_mgr._state.link_handshake_nonce = b"nonce"
    ok = await handshake_mgr.handle_link_sync_resp(0, b"bad-msgpack")
    assert ok is False

@pytest.mark.asyncio
async def test_handle_link_sync_resp_auth_mismatch(handshake_mgr: SerialHandshakeManager):
    nonce = b"A" * 16
    handshake_mgr._state.link_handshake_nonce = nonce
    handshake_mgr._state.link_expected_tag = b"expected"

    # Send different nonce
    sync_pkt = msgspec.msgpack.encode(LinkSyncPacket(nonce=b"B" * 16, tag=b"tag"))
    ok = await handshake_mgr.handle_link_sync_resp(0, sync_pkt)
    assert ok is False

@pytest.mark.asyncio
async def test_fetch_capabilities_send_fail(handshake_mgr: SerialHandshakeManager):
    cast(AsyncMock, handshake_mgr._send_frame).return_value = False
    ok = await handshake_mgr._fetch_capabilities()
    assert ok is False

@pytest.mark.asyncio
async def test_handle_handshake_failure_fatal(handshake_mgr: SerialHandshakeManager):
    handshake_mgr._fatal_threshold = 1
    handshake_mgr._state.handshake_failure_streak = 1
    await handshake_mgr.handle_handshake_failure("test-reason")
    assert handshake_mgr._state.handshake_fatal_count == 1
