import asyncio
import pytest
from unittest.mock import AsyncMock, patch, MagicMock
from typing import Any
from mcubridge.services.file import FileComponent, FileAction
from mcubridge.services.handshake import SerialHandshakeManager
from mcubridge.protocol.structures import (
    FileReadResponsePacket, FileWritePacket, FileRemovePacket, HandshakeConfigPacket,
    LinkSyncPacket, TopicRoute, SerialTimingWindow
)

@pytest.fixture
def mock_serial_flow() -> Any:
    return AsyncMock()

@pytest.fixture
def mock_mqtt_flow() -> Any:
    return AsyncMock()

@pytest.fixture
def mock_state() -> Any:
    state = MagicMock()
    state.mqtt_topic_prefix = "br"
    state.handshake_rate_until = 0.0
    state.link_handshake_nonce = None
    state.handshake_failure_streak = 0
    state.handshake_fatal_count = 0
    state.handshake_attempts = 0
    state.handshake_successes = 0
    state.handshake_failures = 0
    state.handshake_backoff_until = 0.0
    state.handshake_fatal_reason = ""
    state.handshake_fatal_detail = ""
    state.handshake_fatal_unix = 0.0
    state.handshake_last_duration = 0.0
    state.handshake_duration_since_start.return_value = 1.0
    return state

@pytest.fixture
def mock_config() -> Any:
    config = MagicMock()
    config.serial_handshake_fatal_failures = 3
    config.serial_handshake_min_interval = 0.5
    return config

@pytest.mark.asyncio
async def test_file_component_edge_cases(mock_config, mock_state, mock_serial_flow, mock_mqtt_flow) -> None:
    file_comp = FileComponent(mock_config, mock_state, mock_serial_flow, mock_mqtt_flow)
    
    # Test handle_read_response without handler
    await file_comp.handle_read_response(1, b"\x91\xc4\x04data") # Valid msgpack bin
    
    # Test handle_remove failure
    await file_comp.handle_remove(1, b"invalid")

@pytest.mark.asyncio
async def test_handshake_service_edge_cases(mock_config, mock_state, mock_serial_flow, mock_mqtt_flow) -> None:
    timing = SerialTimingWindow(ack_timeout_ms=100, response_timeout_ms=200, retry_limit=3)
    hs = SerialHandshakeManager(
        config=mock_config,
        state=mock_state,
        serial_timing=timing,
        send_frame=mock_serial_flow,
        enqueue_mqtt=mock_mqtt_flow,
        acknowledge_frame=AsyncMock()
    )
    
    # Test various error states and timeouts
    hs.fsm_state = SerialHandshakeManager.STATE_FAULT
    await hs.handle_link_sync_resp(1, b"")
    
    hs.fsm_state = SerialHandshakeManager.STATE_RESETTING
    await hs.handle_link_sync_resp(1, b"")
    
    # Test handle_handshake_failure
    await hs.handle_handshake_failure("test reason")
    await hs.handle_handshake_failure("sync_auth_mismatch")

@pytest.mark.asyncio
async def test_file_component_large_write(mock_config, mock_state, mock_serial_flow, mock_mqtt_flow) -> None:
    file_comp = FileComponent(mock_config, mock_state, mock_serial_flow, mock_mqtt_flow)
    route = MagicMock(spec=TopicRoute)
    route.action = FileAction.WRITE
    route.remainder = ["large.txt"]
    inbound = MagicMock(payload=b"x" * 5000)
    
    # Trigger write path
    await file_comp.handle_mqtt(route, inbound)
