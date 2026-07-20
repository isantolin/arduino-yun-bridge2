"""Targeted coverage gap closure for serial, handshake, structures, security,
and status modules. [SIL-2]"""

from __future__ import annotations

import asyncio
import time
from pathlib import Path
from typing import Any, Iterator, cast
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from mcubridge.config.settings import RuntimeConfig
from mcubridge.protocol import mcubridge_pb2 as pb
from mcubridge.protocol.protocol import Command, Status
from mcubridge.protocol.structures import (
    PendingCommand,
    TopicRoute,
    allows_topic,
    create_queued_publish,
    replace_cloud_publish,
    resolve_cloud_context,
    iter_chunks,
)
from mcubridge.security.security import verify_crypto_integrity
from mcubridge.services.handshake import SerialHandshakeManager, derive_serial_timing
from mcubridge.state.context import RuntimeState, create_runtime_state
from mcubridge.state import status as status_mod
from mcubridge.transport.serial import SerialTransport

# ==============================================================================
# Fixtures
# ==============================================================================


def _make_config(tmp_path: Path) -> RuntimeConfig:
    return RuntimeConfig(
        serial_port="/dev/ttyATH0",
        topic_prefix="br",
        allowed_commands=("*",),
        serial_shared_secret=b"secret123456abcd",
        file_system_root=str(tmp_path / "fs"),
        cloud_spool_dir=str(tmp_path / "spool"),
        allow_non_tmp_paths=True,
    )


@pytest.fixture
def cfg(tmp_path: Path) -> RuntimeConfig:
    return _make_config(tmp_path)


@pytest.fixture
def state(cfg: RuntimeConfig) -> Iterator[RuntimeState]:
    s = create_runtime_state(cfg)
    yield s
    s.cleanup()


@pytest.fixture
def handshake_manager(cfg: RuntimeConfig, state: RuntimeState) -> SerialHandshakeManager:
    timing = derive_serial_timing(cfg)
    return SerialHandshakeManager(
        config=cfg,
        state=state,
        serial_timing=timing,
        send_frame=AsyncMock(return_value=True),
        enqueue_cloud=AsyncMock(),
        acknowledge_frame=AsyncMock(),
    )


# ==============================================================================
# security.py — verify_crypto_integrity (lines 91-93, 97)
# ==============================================================================


def test_verify_crypto_integrity_succeeds() -> None:
    """verify_crypto_integrity() must pass all 3 KAT vectors."""
    assert verify_crypto_integrity() is True


def test_verify_crypto_integrity_sha256_failure(monkeypatch: pytest.MonkeyPatch) -> None:
    """Force SHA-256 KAT failure branch."""
    from mcubridge.security import security as sec_mod

    class _BadHash:
        def update(self, data: bytes) -> None: ...  # noqa: E704
        def finalize(self) -> bytes:
            return b"\x00" * 32

    def _bad_hash_factory(*_args: object, **_kwargs: object) -> _BadHash:
        return _BadHash()

    monkeypatch.setattr(sec_mod.hashes, "Hash", _bad_hash_factory)
    assert sec_mod.verify_crypto_integrity() is False


def test_verify_crypto_integrity_chacha_failure(monkeypatch: pytest.MonkeyPatch) -> None:
    """Force ChaCha20-Poly1305 KAT failure via ValueError."""
    from mcubridge.security import security as sec_mod

    class _BadAEAD:
        def __init__(self, key: bytes) -> None:
            pass

        def encrypt(self, nonce: bytes, data: bytes, aad: Any) -> bytes:
            raise ValueError("forced failure")

    monkeypatch.setattr(sec_mod, "ChaCha20Poly1305", _BadAEAD)
    assert sec_mod.verify_crypto_integrity() is False


# ==============================================================================
# structures.py — iter_chunks empty (line 30), TopicRoute.action (line 66)
# PendingCommand.mark_success/mark_failure (lines 355-366)
# resolve_cloud_context branches (lines 275-288)
# create_queued_publish with expiry (line 260)
# ==============================================================================


def test_iter_chunks_empty() -> None:
    """iter_chunks on empty bytes yields nothing (line 30)."""
    result = list(iter_chunks(b"", 4))
    assert result == []


def test_iter_chunks_non_empty() -> None:
    result = list(iter_chunks(b"abcdefg", 3))
    assert result == [b"abc", b"def", b"g"]


def test_topic_route_action_with_response_segment() -> None:
    """TopicRoute.action returns None when 'response' is in segments (line 65-66)."""
    from mcubridge.protocol.topics import Topic

    route = TopicRoute(
        raw="br/file/response",
        prefix="br",
        topic=Topic.FILE,
        segments=("response",),
    )
    assert route.action is None


def test_topic_route_action_with_value_segment() -> None:
    """TopicRoute.action returns None when 'value' is in segments."""
    from mcubridge.protocol.topics import Topic

    route = TopicRoute(
        raw="br/digital/value",
        prefix="br",
        topic=Topic.DIGITAL,
        segments=("value",),
    )
    assert route.action is None


def test_topic_route_remainder_single_segment() -> None:
    """TopicRoute.remainder returns () when only one segment (line 72)."""
    from mcubridge.protocol.topics import Topic

    route = TopicRoute(
        raw="br/x",
        prefix="br",
        topic=Topic.DIGITAL,
        segments=("write",),
    )
    assert route.remainder == ()


def test_pending_command_mark_success_idempotent() -> None:
    """mark_success sets event exactly once (lines 354-358)."""
    cmd = PendingCommand(command_id=1)
    assert cmd.success is None
    cmd.mark_success(b"payload")
    assert cmd.success is True
    assert cmd.response_payload == b"payload"
    # Second call: completion already set, no crash
    cmd.mark_success(b"other")
    assert cmd.success is True


def test_pending_command_mark_failure_none_status() -> None:
    """mark_failure with None status does not set failure_status on first call (lines 360-365)."""
    cmd = PendingCommand(command_id=2)
    cmd.mark_failure(None)
    assert cmd.success is False
    assert cmd.failure_status is None
    # Second call: completion already set but failure_status CAN still be updated
    cmd.mark_failure(0x42)
    assert cmd.failure_status == 0x42


def test_pending_command_mark_failure_with_status() -> None:
    cmd = PendingCommand(command_id=3)
    cmd.mark_failure(0xFF)
    assert cmd.success is False
    assert cmd.failure_status == 0xFF


def test_create_queued_publish_with_expiry() -> None:
    """message_expiry_interval branch (line 323-324 / 260 in report)."""
    msg = create_queued_publish(
        topic_name="br/test",
        payload=b"data",
        message_expiry_interval=120,
        user_properties=[("k", "v")],
    )
    assert msg.message_expiry_interval == 120
    assert msg.user_properties[0].key == "k"


def test_create_queued_publish_no_expiry() -> None:
    msg = create_queued_publish(topic_name="br/test", payload=b"x")
    assert msg.message_expiry_interval == 0  # default protobuf int


def test_replace_cloud_publish_subscription_identifier() -> None:
    """replace_cloud_publish: subscription_identifier with None (line 260->253)."""
    original = create_queued_publish("t", b"p")
    replaced = replace_cloud_publish(original, subscription_identifier=None)
    assert list(replaced.subscription_identifier) == []


def test_replace_cloud_publish_subscription_identifier_values() -> None:
    original = create_queued_publish("t", b"p")
    replaced = replace_cloud_publish(original, subscription_identifier=[1, 2, 3])
    assert list(replaced.subscription_identifier) == [1, 2, 3]


def test_resolve_cloud_context_none() -> None:
    """context=None returns message unchanged (line 270)."""
    msg = create_queued_publish("original", b"p")
    result = resolve_cloud_context(msg, None)
    assert result.topic_name == "original"


def test_resolve_cloud_context_with_response_topic() -> None:
    """context with response_topic attr (line 274-280)."""
    msg = create_queued_publish("original", b"p")
    ctx = MagicMock()
    ctx.response_topic = "reply/topic"
    ctx.correlation_data = None
    ctx.topic = "req/topic"
    result = resolve_cloud_context(msg, ctx)
    assert result.topic_name == "reply/topic"
    user_prop_keys = [p.key for p in result.user_properties]
    assert "bridge-request-topic" in user_prop_keys


def test_resolve_cloud_context_with_properties_object() -> None:
    """context with nested properties.ResponseTopic (line 276-278)."""
    msg = create_queued_publish("original", b"p")
    ctx = MagicMock()
    ctx.response_topic = None
    ctx.properties = MagicMock()
    ctx.properties.ResponseTopic = "nested/reply"
    ctx.correlation_data = None
    ctx.properties.CorrelationData = None
    ctx.topic = None
    result = resolve_cloud_context(msg, ctx)
    assert result.topic_name == "nested/reply"


def test_resolve_cloud_context_with_correlation_data() -> None:
    """context with correlation_data attr (lines 282-288)."""
    msg = create_queued_publish("original", b"p")
    ctx = MagicMock()
    ctx.response_topic = None
    ctx.properties = None
    ctx.correlation_data = b"\xde\xad\xbe\xef"
    ctx.topic = None
    result = resolve_cloud_context(msg, ctx)
    assert result.correlation_data == b"\xde\xad\xbe\xef"


def test_allows_topic_true() -> None:
    """allows_topic returns True when field is set (line 128)."""

    auth = pb.TopicAuthorization()
    # Find a field that exists in TopicAuthorization and set it
    field_names = [f.name for f in auth.DESCRIPTOR.fields]
    # Use first available field
    first_field = field_names[0]
    setattr(auth, first_field, True)
    # Parse topic/action from field name (e.g. "digital_write" -> topic="digital", action="write")
    parts = first_field.split("_", 1)
    assert len(parts) == 2
    topic_str, action_str = parts[0], parts[1]
    assert allows_topic(auth, topic_str, action_str) is True


def test_allows_topic_unknown() -> None:
    """allows_topic returns False for unknown topic/action (line 129)."""
    auth = pb.TopicAuthorization()
    assert allows_topic(auth, "unknown", "blah") is False


# ==============================================================================
# status.py — CancelledError in _write_tick (lines 34, 37-38)
# ==============================================================================


@pytest.mark.asyncio
async def test_status_writer_cancelled(tmp_path: Path, cfg: RuntimeConfig, state: RuntimeState) -> None:
    """status_writer raises CancelledError cleanly (lines 44-46)."""
    task = asyncio.create_task(status_mod.status_writer(state, interval=0))
    await asyncio.sleep(0.05)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task


@pytest.mark.asyncio
async def test_status_writer_write_tick_cancelled(cfg: RuntimeConfig, state: RuntimeState) -> None:
    """CancelledError during asyncio.shield propagates to caller (line 33-34)."""

    async def _raise_cancelled(*args: Any, **kwargs: Any) -> None:
        raise asyncio.CancelledError

    with patch("mcubridge.state.status.asyncio.to_thread", side_effect=_raise_cancelled):
        task = asyncio.create_task(status_mod.status_writer(state, interval=100))
        await asyncio.sleep(0.05)
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass


def test_write_status_file_oserror(tmp_path: Path, state: RuntimeState) -> None:
    """_write_status_file handles OSError without raising (lines 37-38/60-61)."""
    status = state.build_status_snapshot()
    with patch("mcubridge.state.status.STATUS_FILE", tmp_path / "ro" / "status.json"):
        # Parent dir does not exist and mkdir is mocked to raise
        with patch.object(Path, "mkdir", side_effect=OSError("no space")):
            # Must not raise
            fn = getattr(status_mod, "_write_status_file")
            fn(status)


# ==============================================================================
# serial.py — flow control timeout (lines 362-366), send_raw write exc (404-406)
# _check_baudrate_fallback baud match (297-298), _correlate_frame already resolved
# (248-253), ACK with ProtobufMessage payload (258-259), success status (288-290)
# negotiate_baudrate timeout (418), negotiate_baudrate done future (415-417)
# acknowledge (425)
# ==============================================================================


@pytest.mark.asyncio
async def test_send_raw_flow_control_timeout(cfg: RuntimeConfig, state: RuntimeState) -> None:
    """Flow control wait timeout logs error and continues (lines 362-366)."""
    transport = SerialTransport(cfg, state, None)
    mock_serial = AsyncMock()
    mock_serial.is_open = True
    mock_serial.write = AsyncMock()
    mock_serial.drain = AsyncMock()
    transport.serial = mock_serial

    # Block TX
    state.serial_tx_allowed.clear()

    # Patch asyncio.timeout to raise TimeoutError immediately
    with patch("mcubridge.transport.serial.asyncio.timeout") as mock_to:
        mock_to.return_value.__aenter__ = AsyncMock(side_effect=TimeoutError("flow"))
        mock_to.return_value.__aexit__ = AsyncMock(return_value=False)
        await transport.send_raw(Command.CMD_GET_VERSION.value, b"")
    # should still succeed (write proceeds after timeout)
    state.serial_tx_allowed.set()


@pytest.mark.asyncio
async def test_send_raw_write_exception(cfg: RuntimeConfig, state: RuntimeState) -> None:
    """Serial write exception returns False (lines 404-406)."""
    import serialx

    transport = SerialTransport(cfg, state, None)
    mock_serial = AsyncMock()
    mock_serial.is_open = True
    mock_serial.write = AsyncMock(side_effect=serialx.SerialException("broken"))
    mock_serial.drain = AsyncMock()
    transport.serial = mock_serial
    state.serial_tx_allowed.set()

    result = await transport.send_raw(Command.CMD_GET_VERSION.value, b"")
    assert result is False


@pytest.mark.asyncio
async def test_correlate_frame_already_resolved(cfg: RuntimeConfig, state: RuntimeState) -> None:
    """_correlate_frame skips when pending.success is already set (lines 247-253)."""
    transport = SerialTransport(cfg, state, None)
    pending = PendingCommand(command_id=Command.CMD_GET_VERSION.value)
    pending.mark_success(b"already")
    cast(Any, transport)._current = pending

    cast(Any, transport)._correlate_frame(Status.ACK.value, b"")
    # success remains True (not reset)
    assert pending.success is True


@pytest.mark.asyncio
async def test_correlate_frame_ack_with_protobuf_payload(cfg: RuntimeConfig, state: RuntimeState) -> None:
    """ACK correlation with ProtobufMessage payload path (lines 258-259)."""
    transport = SerialTransport(cfg, state, None)
    cmd_id = Command.CMD_GET_VERSION.value
    pending = PendingCommand(command_id=cmd_id, expected_resp_ids=set())
    cast(Any, transport)._current = pending

    ack_msg = pb.AckPacket(command_id=cmd_id)
    cast(Any, transport)._correlate_frame(Status.ACK.value, ack_msg)
    assert pending.success is True


@pytest.mark.asyncio
async def test_correlate_frame_success_status_code(cfg: RuntimeConfig, state: RuntimeState) -> None:
    """Success status code on pending with no expected resp (lines 288-290)."""
    from mcubridge.transport.serial import SERIAL_SUCCESS_STATUS_CODES

    transport = SerialTransport(cfg, state, None)
    cmd_id = Command.CMD_GET_VERSION.value
    pending = PendingCommand(command_id=cmd_id, expected_resp_ids=set())
    cast(Any, transport)._current = pending

    success_code = next(iter(SERIAL_SUCCESS_STATUS_CODES))
    cast(Any, transport)._correlate_frame(success_code, b"")
    assert pending.success is True


@pytest.mark.asyncio
async def test_check_baudrate_fallback_same_baud(cfg: RuntimeConfig, state: RuntimeState) -> None:
    """_check_baudrate_fallback: baud == safe_baud skips negotiate (line 297)."""
    transport = SerialTransport(cfg, state, None)
    cast(Any, transport)._consecutive_crc_errors = 0
    cast(Any, transport.config).serial_fallback_threshold = 1
    # Set safe_baud == serial_baud to exercise the != guard
    cast(Any, transport.config).serial_safe_baud = transport.config.serial_baud
    with patch.object(transport, "_negotiate_baudrate", new_callable=AsyncMock) as mock_neg:
        await cast(Any, transport)._check_baudrate_fallback()
        mock_neg.assert_not_called()


@pytest.mark.asyncio
async def test_acknowledge_sends_ack(cfg: RuntimeConfig, state: RuntimeState) -> None:
    """acknowledge() calls send_raw with ACK status (line 425)."""
    transport = SerialTransport(cfg, state, None)
    with patch.object(transport, "send_raw", new_callable=AsyncMock) as mock_send:
        await transport.acknowledge(Command.CMD_GET_VERSION.value, 7)
        mock_send.assert_awaited_once()
        args = mock_send.call_args[0]
        assert args[0] == Status.ACK.value


@pytest.mark.asyncio
async def test_negotiate_baudrate_future_already_done(cfg: RuntimeConfig, state: RuntimeState) -> None:
    """_negotiate_baudrate handles already-done future (lines 415-417)."""
    transport = SerialTransport(cfg, state, None)
    mock_serial = AsyncMock()
    mock_serial.is_open = True
    mock_serial.write = AsyncMock()
    mock_serial.drain = AsyncMock()
    transport.serial = mock_serial
    state.serial_tx_allowed.set()

    loop = asyncio.get_running_loop()
    future: asyncio.Future[bool] = loop.create_future()
    future.set_result(True)

    with patch.object(transport, "send_raw", new_callable=AsyncMock, return_value=True):
        with patch.object(loop, "create_future", return_value=future):
            result = await cast(Any, transport)._negotiate_baudrate(115200)
    assert result is True


# ==============================================================================
# handshake.py — success stats (lines 505-514), fetch_capabilities (362-379),
# handle_capabilities_resp future done (382-384), parse_capabilities protobuf (390)
# handle_link_reset_resp (400-403), wait_for_link_sync already sync (464)
# _synchronize_attempt fault race (223), nonce cleared when None (469)
# ==============================================================================


@pytest.mark.asyncio
async def test_handle_capabilities_resp_sets_future(
    handshake_manager: SerialHandshakeManager,
) -> None:
    """handle_capabilities_resp sets the capabilities future result (lines 382-384)."""
    loop = asyncio.get_running_loop()
    future: asyncio.Future[Any] = loop.create_future()
    cast(Any, handshake_manager)._capabilities_future = future

    result = await handshake_manager.handle_capabilities_resp(1, b"")
    assert result is True
    assert future.done()


@pytest.mark.asyncio
async def test_handle_capabilities_resp_future_already_done(
    handshake_manager: SerialHandshakeManager,
) -> None:
    """handle_capabilities_resp when future already done doesn't set again."""
    loop = asyncio.get_running_loop()
    future: asyncio.Future[Any] = loop.create_future()
    future.set_result(b"done")
    cast(Any, handshake_manager)._capabilities_future = future

    result = await handshake_manager.handle_capabilities_resp(1, b"extra")
    assert result is True
    assert future.result() == b"done"  # unchanged


@pytest.mark.asyncio
async def test_parse_capabilities_with_protobuf(handshake_manager: SerialHandshakeManager, state: RuntimeState) -> None:
    """_parse_capabilities accepts ProtobufMessage directly (line 389-390)."""
    caps = pb.Capabilities()
    cast(Any, handshake_manager)._parse_capabilities(caps)
    assert state.mcu_capabilities is not None


@pytest.mark.asyncio
async def test_parse_capabilities_invalid(
    handshake_manager: SerialHandshakeManager,
) -> None:
    """_parse_capabilities with invalid bytes logs error and doesn't raise."""
    cast(Any, handshake_manager)._parse_capabilities(b"\xff\xff\xff")


@pytest.mark.asyncio
async def test_handle_link_reset_resp(
    handshake_manager: SerialHandshakeManager,
) -> None:
    """handle_link_reset_resp always returns True (lines 400-403)."""
    result = await handshake_manager.handle_link_reset_resp(1, b"payload")
    assert result is True

    result2 = await handshake_manager.handle_link_reset_resp(2, pb.LinkSync())
    assert result2 is True


@pytest.mark.asyncio
async def test_wait_for_link_sync_already_synchronized(
    handshake_manager: SerialHandshakeManager, state: RuntimeState
) -> None:
    """_wait_for_link_sync_confirmation skips wait if already synchronized (line 462-464)."""
    state.mark_synchronized()
    nonce = b"\x01" * 16
    result = await cast(Any, handshake_manager)._wait_for_link_sync_confirmation(nonce)
    assert result is True


@pytest.mark.asyncio
@pytest.mark.timeout(3)
async def test_wait_for_link_sync_timeout(handshake_manager: SerialHandshakeManager, state: RuntimeState) -> None:
    """_wait_for_link_sync_confirmation times out and returns False."""
    # Not synchronized, event never set → timeout with minimal timing
    cast(Any, handshake_manager)._timing.response_timeout_ms = 100
    nonce = b"\x02" * 16
    result = await cast(Any, handshake_manager)._wait_for_link_sync_confirmation(nonce)
    assert result is False


@pytest.mark.asyncio
async def test_clear_handshake_expectations_nonce_none(
    handshake_manager: SerialHandshakeManager, state: RuntimeState
) -> None:
    """clear_handshake_expectations with None nonce/tag (line 469)."""
    state.link_handshake_nonce = None
    state.link_expected_tag = None
    handshake_manager.clear_handshake_expectations()  # should not raise
    assert state.link_handshake_nonce is None


@pytest.mark.asyncio
@pytest.mark.timeout(3)
async def test_synchronize_attempt_fault_already_set(
    handshake_manager: SerialHandshakeManager, state: RuntimeState
) -> None:
    """Fault race guard: if FSM is already FAULT after sync, return False (line 207-208)."""
    from mcubridge.services.handshake import HandshakeState

    cast(Any, handshake_manager)._timing.response_timeout_ms = 100
    send_frame: AsyncMock = cast(Any, handshake_manager)._send_frame

    call_count = 0

    async def _send_and_fault(cmd: int, payload: Any) -> bool:
        nonlocal call_count
        call_count += 1
        if call_count >= 2:
            # Simulate MCU response that transitions to FAULT concurrently
            cast(Any, handshake_manager)._set_fsm_state(HandshakeState.FAULT)
        return True

    send_frame.side_effect = _send_and_fault

    result = await cast(Any, handshake_manager)._synchronize_attempt()
    assert result is False


@pytest.mark.asyncio
async def test_handle_handshake_success_resets_streak(
    handshake_manager: SerialHandshakeManager, state: RuntimeState
) -> None:
    """_handle_handshake_success resets streak and updates metrics (lines 505-514)."""
    state.handshake_failure_streak = 5
    state.handshake_successes = 0
    state.mark_synchronized()

    # Patch _publish_handshake_event to avoid state snapshot issues
    with patch.object(handshake_manager, "_publish_handshake_event", new_callable=AsyncMock):
        await cast(Any, handshake_manager)._handle_handshake_success()
    assert state.handshake_failure_streak == 0
    assert state.handshake_successes == 1


@pytest.mark.asyncio
@pytest.mark.timeout(3)
async def test_fetch_capabilities_send_fails(
    handshake_manager: SerialHandshakeManager,
) -> None:
    """_fetch_capabilities returns False when send_frame returns False."""
    send_frame: AsyncMock = cast(Any, handshake_manager)._send_frame
    send_frame.return_value = False

    # Patch tenacity to stop after 1 attempt (prevent long retry loop)
    import tenacity as _tenacity

    with patch(
        "mcubridge.services.handshake.tenacity.stop_after_attempt",
        return_value=_tenacity.stop_after_attempt(1),
    ):
        result = await cast(Any, handshake_manager)._fetch_capabilities()
    assert result is False


@pytest.mark.asyncio
async def test_handle_link_sync_resp_sync_already_confirmed(
    handshake_manager: SerialHandshakeManager, state: RuntimeState
) -> None:
    """handle_link_sync_resp: nonce_mismatch returns False silently (line 296-302)."""
    import os

    nonce = os.urandom(16)
    state.link_handshake_nonce = nonce

    # Send a DIFFERENT nonce in the response
    wrong_nonce = bytes(b ^ 0xFF for b in nonce)
    bad_payload = pb.LinkSync(nonce=wrong_nonce, tag=b"\x00" * 16).SerializeToString()

    result = await handshake_manager.handle_link_sync_resp(1, bad_payload)
    assert result is False


@pytest.mark.asyncio
async def test_handle_link_sync_resp_rate_limited(
    handshake_manager: SerialHandshakeManager, state: RuntimeState
) -> None:
    """Rate limiting rejects LINK_SYNC_RESP (lines 252-263)."""
    import os

    cast(Any, handshake_manager)._config.serial_handshake_min_interval = 60.0
    state.handshake_rate_until = time.monotonic() + 60.0
    nonce = os.urandom(16)
    state.link_handshake_nonce = nonce

    payload = pb.LinkSync(nonce=nonce, tag=b"\x00" * 16).SerializeToString()
    result = await handshake_manager.handle_link_sync_resp(1, payload)
    assert result is False
    assert state.last_handshake_error == "sync_rate_limited"


@pytest.mark.asyncio
@pytest.mark.timeout(3)
async def test_synchronize_nonce_already_cleared_no_failure(
    handshake_manager: SerialHandshakeManager, state: RuntimeState
) -> None:
    """_synchronize_attempt: when nonce cleared before timeout, skip failure report (line 227)."""
    cast(Any, handshake_manager)._timing.response_timeout_ms = 100

    call_count = 0

    async def _send_reset_and_clear(cmd: int, payload: Any) -> bool:
        nonlocal call_count
        call_count += 1
        if call_count >= 2:
            # Clear nonce to simulate concurrent external clear
            state.link_handshake_nonce = None
        return True

    cast(Any, handshake_manager)._send_frame.side_effect = _send_reset_and_clear

    result = await cast(Any, handshake_manager)._synchronize_attempt()
    assert result is False  # timed out or fault


@pytest.mark.asyncio
@pytest.mark.timeout(5)
async def test_synchronize_attempt_success_state_transition(
    handshake_manager: SerialHandshakeManager, state: RuntimeState
) -> None:
    """_synchronize_attempt succeeds when link_sync_event is set in time."""
    cast(Any, handshake_manager)._timing.response_timeout_ms = 500

    sent_nonces: list[bytes] = []

    async def _send_and_complete(cmd: int, payload: Any) -> bool:
        if hasattr(payload, "nonce"):
            sent_nonces.append(bytes(payload.nonce))
        if cmd == Command.CMD_LINK_SYNC.value:
            # Simulate MCU accepting immediately
            state.mark_synchronized()
            state.link_sync_event.set()
        return True

    cast(Any, handshake_manager)._send_frame.side_effect = _send_and_complete

    result = await cast(Any, handshake_manager)._synchronize_attempt()
    assert result is True
