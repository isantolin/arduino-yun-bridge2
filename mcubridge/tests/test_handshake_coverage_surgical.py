"""Surgical unit test suite for services/handshake.py covering edge paths and error branches."""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from unittest.mock import AsyncMock

from google.protobuf.message import Message as ProtobufMessage
import pytest
from pytest_mock import MockerFixture

from mcubridge.config.settings import RuntimeConfig
import mcubridge.protocol.mcubridge_pb2 as pb
from mcubridge.services.handshake import HandshakeState, SerialHandshakeManager, derive_serial_timing
from mcubridge.state.context import RuntimeState, create_runtime_state


def _make_config() -> RuntimeConfig:
    return RuntimeConfig(
        serial_port="/dev/ttyMCU",
        serial_baud=115200,
        serial_safe_baud=9600,
        serial_shared_secret=b"testsharedsecret",
        allow_non_tmp_paths=True,
    )


@pytest.fixture
def mock_config() -> RuntimeConfig:
    return _make_config()


@pytest.fixture
def mock_state(mock_config: RuntimeConfig) -> RuntimeState:
    return create_runtime_state(mock_config)


def _make_handshake_manager(
    config: RuntimeConfig,
    state: RuntimeState,
    send_frame: AsyncMock | None = None,
    acknowledge_frame: AsyncMock | None = None,
    enqueue_cloud: AsyncMock | None = None,
) -> SerialHandshakeManager:
    return SerialHandshakeManager(
        config=config,
        state=state,
        serial_timing=derive_serial_timing(config),
        send_frame=send_frame or AsyncMock(return_value=True),
        acknowledge_frame=acknowledge_frame or AsyncMock(),
        enqueue_cloud=enqueue_cloud or AsyncMock(),
    )


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_synchronize_attempt_send_frame_failure(mock_config: RuntimeConfig, mock_state: RuntimeState) -> None:
    mock_send = AsyncMock(return_value=False)
    mgr = _make_handshake_manager(mock_config, mock_state, send_frame=mock_send)

    sync_attempt: Callable[[], Awaitable[bool]] = getattr(mgr, "_synchronize_attempt")
    res = await sync_attempt()
    assert res is False


@pytest.mark.asyncio
async def test_synchronize_attempt_timeout_confirmation(
    mock_config: RuntimeConfig, mock_state: RuntimeState, mocker: MockerFixture
) -> None:
    mock_send = AsyncMock(return_value=True)
    mgr = _make_handshake_manager(mock_config, mock_state, send_frame=mock_send)

    mocker.patch.object(mgr, "_wait_for_link_sync_confirmation", new_callable=AsyncMock, return_value=False)
    sync_attempt: Callable[[], Awaitable[bool]] = getattr(mgr, "_synchronize_attempt")
    res = await sync_attempt()
    assert res is False


@pytest.mark.asyncio
async def test_fetch_capabilities_send_failure_retries_exhausted(
    mock_config: RuntimeConfig, mock_state: RuntimeState
) -> None:
    mock_send = AsyncMock(return_value=False)
    mgr = _make_handshake_manager(mock_config, mock_state, send_frame=mock_send)

    fetch_caps: Callable[[], Awaitable[bool]] = getattr(mgr, "_fetch_capabilities")
    res = await fetch_caps()
    assert res is False


@pytest.mark.asyncio
async def test_fetch_capabilities_future_timeout_exception(
    mock_config: RuntimeConfig, mock_state: RuntimeState, mocker: MockerFixture
) -> None:
    mock_send = AsyncMock(return_value=True)
    mgr = _make_handshake_manager(mock_config, mock_state, send_frame=mock_send)

    mocker.patch("asyncio.timeout", side_effect=TimeoutError("Simulated timeout"))

    def _zero_wait(_rs: object) -> float:
        return 0.0

    mocker.patch("tenacity.wait_exponential", return_value=_zero_wait)
    fetch_caps: Callable[[], Awaitable[bool]] = getattr(mgr, "_fetch_capabilities")
    res = await fetch_caps()
    assert res is False


@pytest.mark.asyncio
async def test_handle_link_sync_resp_nonce_mismatch(mock_config: RuntimeConfig, mock_state: RuntimeState) -> None:
    mgr = _make_handshake_manager(mock_config, mock_state)
    mock_state.link_handshake_nonce = b"expectednonce12"

    payload = pb.LinkSync(nonce=b"wrongnonce1234", tag=b"sometag")
    res = await mgr.handle_link_sync_resp(1, payload)
    assert res is False


@pytest.mark.asyncio
async def test_handle_link_sync_resp_tag_mismatch(mock_config: RuntimeConfig, mock_state: RuntimeState) -> None:
    mgr = _make_handshake_manager(mock_config, mock_state)
    nonce = b"validnonce1234"
    mock_state.link_handshake_nonce = nonce
    mock_state.link_expected_tag = b"correcttag1234"

    payload = pb.LinkSync(nonce=nonce, tag=b"badtag12345678")
    res = await mgr.handle_link_sync_resp(1, payload)
    assert res is False
    assert mock_state.last_handshake_error == "sync_auth_mismatch"


@pytest.mark.asyncio
async def test_handle_link_sync_resp_success(
    mock_config: RuntimeConfig, mock_state: RuntimeState, mocker: MockerFixture
) -> None:
    mock_ack = AsyncMock()
    mgr = _make_handshake_manager(mock_config, mock_state, acknowledge_frame=mock_ack)

    nonce = b"validnonce1234"
    mock_state.link_handshake_nonce = nonce
    expected_tag = SerialHandshakeManager.calculate_handshake_tag(mock_config.serial_shared_secret, nonce)
    mock_state.link_expected_tag = expected_tag

    mocker.patch.object(mgr, "_fetch_capabilities_with_delay", new_callable=AsyncMock)

    payload = pb.LinkSync(nonce=nonce, tag=expected_tag)
    res = await mgr.handle_link_sync_resp(1, payload)

    assert res is True
    assert mock_state.is_synchronized
    assert mock_state.link_session_key is not None


@pytest.mark.asyncio
async def test_handle_capabilities_resp_invalid_payload(mock_config: RuntimeConfig, mock_state: RuntimeState) -> None:
    mgr = _make_handshake_manager(mock_config, mock_state)
    fut: asyncio.Future[bytes | ProtobufMessage] = asyncio.Future()
    setattr(mgr, "_capabilities_future", fut)

    res = await mgr.handle_capabilities_resp(1, b"invalid proto bytes")
    assert res is True
    assert fut.done()
    assert fut.result() == b"invalid proto bytes"


@pytest.mark.asyncio
async def test_handle_capabilities_resp_success(mock_config: RuntimeConfig, mock_state: RuntimeState) -> None:
    mgr = _make_handshake_manager(mock_config, mock_state)
    fut: asyncio.Future[bytes | ProtobufMessage] = asyncio.Future()
    setattr(mgr, "_capabilities_future", fut)

    cap = pb.Capabilities(watchdog=True, eeprom=False)
    res = await mgr.handle_capabilities_resp(1, cap)
    assert res is True
    assert fut.done()
    assert fut.result() == cap


def test_clear_handshake_expectations(mock_config: RuntimeConfig, mock_state: RuntimeState) -> None:
    mgr = _make_handshake_manager(mock_config, mock_state)
    mock_state.link_handshake_nonce = b"12345678"
    mock_state.link_expected_tag = b"abcdefgh"
    mock_state.link_nonce_length = 8

    mgr.clear_handshake_expectations()
    assert mock_state.link_handshake_nonce is None
    assert mock_state.link_expected_tag is None
    assert mock_state.link_nonce_length == 0


@pytest.mark.asyncio
async def test_handle_link_reset_resp(mock_config: RuntimeConfig, mock_state: RuntimeState) -> None:
    mgr = _make_handshake_manager(mock_config, mock_state)
    res = await mgr.handle_link_reset_resp(1, b"")
    assert res is True


@pytest.mark.asyncio
async def test_handle_handshake_failure_max_retries(mock_config: RuntimeConfig, mock_state: RuntimeState) -> None:
    mgr = _make_handshake_manager(mock_config, mock_state)
    mock_state.handshake_attempts = 100
    mock_state.handshake_failure_streak = mock_config.serial_handshake_fatal_failures

    # Should transition to FAULT state
    await mgr.handle_handshake_failure("max_attempts_exceeded")
    assert mgr.fsm_state == HandshakeState.FAULT


@pytest.mark.asyncio
async def test_publish_handshake_event_cloud_enqueue(mock_config: RuntimeConfig, mock_state: RuntimeState) -> None:
    mock_enqueue = AsyncMock()
    mgr = _make_handshake_manager(mock_config, mock_state, enqueue_cloud=mock_enqueue)

    publish_event: Callable[..., Awaitable[None]] = getattr(mgr, "_publish_handshake_event")
    await publish_event("sync_success")
    mock_enqueue.assert_called_once()
    published_msg = mock_enqueue.call_args[0][0]
    assert published_msg.topic_name == "br/system/bridge/handshake/value"
    snapshot = pb.HandshakeSnapshot.FromString(published_msg.payload)
    assert snapshot.event == "sync_success"

