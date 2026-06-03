"""Serial transport unit tests — SIL-2 coverage."""

from __future__ import annotations

import asyncio
import time
from binascii import crc32
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from cobs import cobs

from mcubridge.config.settings import RuntimeConfig
from mcubridge.protocol import protocol
from mcubridge.protocol.protocol import (
    Command,
    Status,
    expected_responses,
    response_to_request,
)
from mcubridge.protocol.structures import PendingCommand
from mcubridge.security.security import generate_nonce_with_counter
from mcubridge.state.context import create_runtime_state
from mcubridge.transport.serial import SerialTransport


def _make_config() -> RuntimeConfig:
    import os

    fs_root = f".tmp_tests/mcubridge-test-fs-transport-{os.getpid()}-{time.time_ns()}"
    return RuntimeConfig(
        serial_port="/dev/null",
        serial_baud=protocol.DEFAULT_BAUDRATE,
        serial_safe_baud=protocol.DEFAULT_BAUDRATE,
        mqtt_host="localhost",
        mqtt_port=1883,
        mqtt_topic=protocol.MQTT_DEFAULT_TOPIC_PREFIX,
        allowed_commands=(),
        file_system_root=fs_root,
        serial_retry_timeout=0.05,
        serial_response_timeout=0.1,
        serial_retry_attempts=1,
        serial_shared_secret=b"test_secret_key_",
        allow_non_tmp_paths=True,
    )


def _mock_writer() -> MagicMock:
    w = MagicMock()
    w.is_closing.return_value = False
    w.drain = AsyncMock()
    return w


def _valid_packet(command_id: int, seq_id: int = 1, payload: bytes = b"") -> bytes:
    """Build a COBS-encoded packet with correct CRC for use in _process_packet."""
    from mcubridge.protocol.frame import build_frame

    body = build_frame(
        command_id=command_id,
        sequence_id=seq_id,
        payload=payload,
        nonce=b"\x00" * 12,
        session_key=None,
    )
    crc_val = crc32(body) & 0xFFFFFFFF
    return cobs.encode(body + crc_val.to_bytes(4, "little"))


# ---------------------------------------------------------------------------
# reset()
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_reset_no_pending() -> None:
    config = _make_config()
    state = create_runtime_state(config)
    try:
        serial = SerialTransport(config, state, None)
        await serial.reset()  # Nothing pending — just acquires lock and exits
        assert serial._current is None
    finally:
        state.cleanup()


@pytest.mark.asyncio
async def test_reset_with_pending() -> None:
    config = _make_config()
    state = create_runtime_state(config)
    try:
        serial = SerialTransport(config, state, None)
        pending = PendingCommand(command_id=Command.CMD_GET_VERSION.value, expected_resp_ids=set())
        serial._current = pending
        await serial.reset()
        assert pending.success is False
        assert serial._current is None
    finally:
        state.cleanup()


# ---------------------------------------------------------------------------
# stop()
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_stop_no_writer() -> None:
    config = _make_config()
    state = create_runtime_state(config)
    try:
        serial = SerialTransport(config, state, None)
        await serial.stop()
        assert serial._stop_event.is_set()
    finally:
        state.cleanup()


@pytest.mark.asyncio
async def test_stop_with_writer() -> None:
    config = _make_config()
    state = create_runtime_state(config)
    try:
        serial = SerialTransport(config, state, None)
        mock_w = _mock_writer()
        serial.writer = mock_w
        await serial.stop()
        assert serial._stop_event.is_set()
        mock_w.close.assert_called_once()
    finally:
        state.cleanup()


# ---------------------------------------------------------------------------
# _active_transport()
# ---------------------------------------------------------------------------


def test_active_transport_no_writer() -> None:
    config = _make_config()
    state = create_runtime_state(config)
    try:
        serial = SerialTransport(config, state, None)
        with pytest.raises(RuntimeError, match="Serial writer inactive"):
            serial._active_transport()
    finally:
        state.cleanup()


def test_active_transport_closing_writer() -> None:
    config = _make_config()
    state = create_runtime_state(config)
    try:
        serial = SerialTransport(config, state, None)
        mock_w = MagicMock()
        mock_w.is_closing.return_value = True
        serial.writer = mock_w
        with pytest.raises(RuntimeError, match="Serial writer inactive"):
            serial._active_transport()
    finally:
        state.cleanup()


# ---------------------------------------------------------------------------
# _switch_local_baudrate()
# ---------------------------------------------------------------------------


def test_switch_local_baudrate_error() -> None:
    class _FailingSerial:
        def __setattr__(self, name: str, val: object) -> None:
            raise AttributeError("no uart access")

    class _FailingTransport:
        serial = _FailingSerial()

    config = _make_config()
    state = create_runtime_state(config)
    try:
        serial = SerialTransport(config, state, None)
        with patch.object(serial, "_active_transport", return_value=_FailingTransport()):
            with pytest.raises(RuntimeError, match="UART access failed"):
                serial._switch_local_baudrate(9600)
    finally:
        state.cleanup()


# ---------------------------------------------------------------------------
# send() — high-level unified send
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_send_no_writer_returns_false() -> None:
    config = _make_config()
    state = create_runtime_state(config)
    try:
        serial = SerialTransport(config, state, None)
        result = await serial.send(Command.CMD_GET_VERSION.value, b"")
        assert result is False
    finally:
        state.cleanup()


@pytest.mark.asyncio
async def test_send_not_tracked_calls_send_raw() -> None:
    """Response commands are not tracked; they go straight to send_raw."""
    config = _make_config()
    state = create_runtime_state(config)
    try:
        serial = SerialTransport(config, state, None)
        mock_w = _mock_writer()
        serial.writer = mock_w
        state.serial_tx_allowed.set()

        # CMD_GET_VERSION_RESP has no expected_responses and is not ACK_ONLY
        result = await serial.send(Command.CMD_GET_VERSION_RESP.value, b"")
        assert result is True
        mock_w.write.assert_called_once()
    finally:
        state.cleanup()


@pytest.mark.asyncio
async def test_send_handshake_not_tracked() -> None:
    """Handshake commands bypass tracking and go straight to send_raw."""
    config = _make_config()
    state = create_runtime_state(config)
    try:
        serial = SerialTransport(config, state, None)
        mock_w = _mock_writer()
        serial.writer = mock_w
        state.serial_tx_allowed.set()

        result = await serial.send(Command.CMD_LINK_SYNC.value, b"")
        assert result is True
        mock_w.write.assert_called_once()
    finally:
        state.cleanup()


@pytest.mark.asyncio
async def test_send_tracked_send_raw_fails() -> None:
    """Tracked send returns False when write raises (FatalSerialError path)."""
    config = _make_config()
    state = create_runtime_state(config)
    try:
        serial = SerialTransport(config, state, None)
        mock_w = _mock_writer()
        mock_w.write.side_effect = OSError("write error")
        serial.writer = mock_w
        state.serial_tx_allowed.set()

        result = await serial.send(Command.CMD_GET_VERSION.value, b"")
        assert result is False
    finally:
        state.cleanup()


@pytest.mark.asyncio
async def test_send_tracked_timeout() -> None:
    """Tracked send returns False when response times out (RetryableSerialError path)."""
    config = _make_config()
    state = create_runtime_state(config)
    try:
        serial = SerialTransport(config, state, None)
        mock_w = _mock_writer()
        serial.writer = mock_w
        state.serial_tx_allowed.set()

        # No _correlate_frame call → completion.wait() times out after 0.1 s
        result = await serial.send(Command.CMD_GET_VERSION.value, b"")
        assert result is False
    finally:
        state.cleanup()


@pytest.mark.asyncio
async def test_send_tracked_success() -> None:
    """Tracked send returns response payload when completion is marked as success."""
    config = _make_config()
    state = create_runtime_state(config)
    try:
        serial = SerialTransport(config, state, None)
        mock_w = _mock_writer()
        serial.writer = mock_w
        state.serial_tx_allowed.set()

        async def _mark_success_after_start() -> None:
            await asyncio.sleep(0.01)
            if serial._current is not None:
                serial._current.mark_success(b"version_data")

        asyncio.create_task(_mark_success_after_start())
        result = await serial.send(Command.CMD_GET_VERSION.value, b"")
        assert result == b"version_data"
    finally:
        state.cleanup()


# ---------------------------------------------------------------------------
# send_raw()
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_send_raw_no_writer() -> None:
    config = _make_config()
    state = create_runtime_state(config)
    try:
        serial = SerialTransport(config, state, None)
        result = await serial.send_raw(Command.CMD_GET_VERSION.value, b"")
        assert result is False
    finally:
        state.cleanup()


@pytest.mark.asyncio
async def test_send_raw_success() -> None:
    config = _make_config()
    state = create_runtime_state(config)
    try:
        serial = SerialTransport(config, state, None)
        mock_w = _mock_writer()
        serial.writer = mock_w
        state.serial_tx_allowed.set()

        result = await serial.send_raw(Command.CMD_GET_VERSION_RESP.value, b"")
        assert result is True
        mock_w.write.assert_called_once()
        mock_w.drain.assert_awaited_once()
    finally:
        state.cleanup()


@pytest.mark.asyncio
async def test_send_raw_write_error() -> None:
    config = _make_config()
    state = create_runtime_state(config)
    try:
        serial = SerialTransport(config, state, None)
        mock_w = _mock_writer()
        mock_w.write.side_effect = OSError("broken pipe")
        serial.writer = mock_w
        state.serial_tx_allowed.set()

        result = await serial.send_raw(Command.CMD_GET_VERSION.value, b"")
        assert result is False
    finally:
        state.cleanup()


@pytest.mark.asyncio
async def test_send_raw_tx_not_allowed_then_allowed() -> None:
    """TX flow control: send_raw waits until serial_tx_allowed is set."""
    config = _make_config()
    state = create_runtime_state(config)
    try:
        serial = SerialTransport(config, state, None)
        mock_w = _mock_writer()
        serial.writer = mock_w
        # Do NOT set serial_tx_allowed yet

        async def _set_allowed() -> None:
            await asyncio.sleep(0.01)
            state.serial_tx_allowed.set()

        asyncio.create_task(_set_allowed())
        result = await serial.send_raw(Command.CMD_GET_VERSION_RESP.value, b"")
        assert result is True
    finally:
        state.cleanup()


@pytest.mark.asyncio
async def test_send_raw_synchronized_generates_nonce() -> None:
    """When state is synchronized send_raw advances the nonce counter."""
    config = _make_config()
    state = create_runtime_state(config)
    try:
        serial = SerialTransport(config, state, None)
        mock_w = _mock_writer()
        serial.writer = mock_w
        state.serial_tx_allowed.set()
        state.mark_synchronized()
        state.link_session_key = b"A" * 32
        initial_counter = state.link_nonce_counter

        # CMD_SET_PIN_MODE (0x50) is not a system command — nonce is generated
        result = await serial.send_raw(Command.CMD_SET_PIN_MODE.value, b"")
        assert result is True
        assert state.link_nonce_counter > initial_counter
    finally:
        state.cleanup()


# ---------------------------------------------------------------------------
# _correlate_frame()
# ---------------------------------------------------------------------------


def test_correlate_frame_no_pending() -> None:
    config = _make_config()
    state = create_runtime_state(config)
    try:
        serial = SerialTransport(config, state, None)
        serial._correlate_frame(Status.ACK.value, b"")  # No-op; _current is None
    finally:
        state.cleanup()


def test_correlate_frame_ack_match_no_expected_resp() -> None:
    config = _make_config()
    state = create_runtime_state(config)
    try:
        serial = SerialTransport(config, state, None)
        pending = PendingCommand(command_id=Command.CMD_GET_VERSION.value, expected_resp_ids=set())
        serial._current = pending

        serial._correlate_frame(Status.ACK.value, b"")
        assert pending.ack_received is True
        assert pending.success is True
    finally:
        state.cleanup()


def test_correlate_frame_ack_with_payload() -> None:
    from mcubridge.protocol import mcubridge_pb2 as pb

    config = _make_config()
    state = create_runtime_state(config)
    try:
        serial = SerialTransport(config, state, None)
        cmd_id = Command.CMD_GET_VERSION.value
        pending = PendingCommand(command_id=cmd_id, expected_resp_ids=set())
        serial._current = pending

        ack_payload = pb.AckPacket(command_id=cmd_id).SerializeToString()
        serial._correlate_frame(Status.ACK.value, ack_payload)
        assert pending.ack_received is True
        assert pending.success is True
    finally:
        state.cleanup()


def test_correlate_frame_response_to_request() -> None:
    config = _make_config()
    state = create_runtime_state(config)
    try:
        serial = SerialTransport(config, state, None)
        pending = PendingCommand(
            command_id=Command.CMD_GET_VERSION.value,
            expected_resp_ids={Command.CMD_GET_VERSION_RESP.value},
        )
        serial._current = pending

        serial._correlate_frame(Command.CMD_GET_VERSION_RESP.value, b"resp_data")
        assert pending.success is True
        assert pending.response_payload == b"resp_data"
    finally:
        state.cleanup()


def test_correlate_frame_failure_status() -> None:
    from mcubridge.config.const import SERIAL_FAILURE_STATUS_CODES

    config = _make_config()
    state = create_runtime_state(config)
    try:
        serial = SerialTransport(config, state, None)
        failure_code = next(iter(SERIAL_FAILURE_STATUS_CODES))
        pending = PendingCommand(command_id=Command.CMD_GET_VERSION.value, expected_resp_ids=set())
        serial._current = pending

        serial._correlate_frame(failure_code, b"")
        assert pending.success is False
        assert pending.failure_status == failure_code
    finally:
        state.cleanup()


def test_correlate_frame_success_status() -> None:
    from mcubridge.config.const import SERIAL_SUCCESS_STATUS_CODES

    config = _make_config()
    state = create_runtime_state(config)
    try:
        serial = SerialTransport(config, state, None)
        ok_code = next(iter(SERIAL_SUCCESS_STATUS_CODES))
        pending = PendingCommand(command_id=Command.CMD_GET_VERSION.value, expected_resp_ids=set())
        serial._current = pending

        serial._correlate_frame(ok_code, b"")
        assert pending.success is True
    finally:
        state.cleanup()


# ---------------------------------------------------------------------------
# _check_baudrate_fallback()
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_check_baudrate_fallback_below_threshold() -> None:
    config = _make_config()
    state = create_runtime_state(config)
    try:
        serial = SerialTransport(config, state, None)
        serial._consecutive_crc_errors = 0
        await serial._check_baudrate_fallback()
        assert serial._consecutive_crc_errors == 1
    finally:
        state.cleanup()


@pytest.mark.asyncio
async def test_check_baudrate_fallback_at_threshold() -> None:
    """At threshold, reset error count (same baud → no negotiation)."""
    config = _make_config()
    state = create_runtime_state(config)
    try:
        serial = SerialTransport(config, state, None)
        serial._consecutive_crc_errors = config.serial_fallback_threshold - 1
        await serial._check_baudrate_fallback()
        assert serial._consecutive_crc_errors == 0
    finally:
        state.cleanup()


# ---------------------------------------------------------------------------
# _read_loop()
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_read_loop_incomplete_read_breaks() -> None:
    config = _make_config()
    state = create_runtime_state(config)
    try:
        serial = SerialTransport(config, state, None)
        mock_reader = AsyncMock()
        mock_reader.readuntil = AsyncMock(side_effect=asyncio.IncompleteReadError(b"", 0))

        await serial._read_loop(mock_reader)  # Returns after break
    finally:
        state.cleanup()


@pytest.mark.asyncio
async def test_read_loop_limit_overrun_then_breaks() -> None:
    config = _make_config()
    state = create_runtime_state(config)
    try:
        serial = SerialTransport(config, state, None)
        mock_reader = AsyncMock()
        mock_reader.readuntil = AsyncMock(
            side_effect=[
                asyncio.LimitOverrunError("overflow", 0),
                asyncio.IncompleteReadError(b"", 0),
            ]
        )
        mock_reader.read = AsyncMock(return_value=b"junk")

        await serial._read_loop(mock_reader)
        assert state.serial_decode_errors >= 1
    finally:
        state.cleanup()


@pytest.mark.asyncio
async def test_read_loop_oserror_breaks() -> None:
    config = _make_config()
    state = create_runtime_state(config)
    try:
        serial = SerialTransport(config, state, None)
        mock_reader = AsyncMock()
        mock_reader.readuntil = AsyncMock(side_effect=OSError("read error"))

        await serial._read_loop(mock_reader)  # OSError handler breaks
    finally:
        state.cleanup()


@pytest.mark.asyncio
async def test_read_loop_dispatches_valid_packet() -> None:
    """_read_loop strips delimiter and calls _process_packet for a non-empty view."""
    config = _make_config()
    state = create_runtime_state(config)
    try:
        serial = SerialTransport(config, state, None)
        encoded = _valid_packet(Command.CMD_GET_VERSION_RESP.value)
        mock_reader = AsyncMock()
        mock_reader.readuntil = AsyncMock(
            side_effect=[
                encoded + protocol.FRAME_DELIMITER,
                asyncio.IncompleteReadError(b"", 0),
            ]
        )

        with patch.object(serial, "_process_packet", new_callable=AsyncMock) as mock_pp:
            await serial._read_loop(mock_reader)
            mock_pp.assert_awaited_once()
    finally:
        state.cleanup()


# ---------------------------------------------------------------------------
# _process_packet()
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_process_packet_cobs_error() -> None:
    config = _make_config()
    state = create_runtime_state(config)
    try:
        serial = SerialTransport(config, state, None)
        bad_cobs = b"\xff\xff\xff"  # Invalid COBS

        await serial._process_packet(bad_cobs)
        assert state.serial_decode_errors >= 1
    finally:
        state.cleanup()


@pytest.mark.asyncio
async def test_process_packet_crc_mismatch() -> None:
    config = _make_config()
    state = create_runtime_state(config)
    try:
        serial = SerialTransport(config, state, None)
        from mcubridge.protocol.frame import build_frame

        body = build_frame(
            command_id=Command.CMD_GET_VERSION_RESP.value,
            sequence_id=1,
            payload=b"",
            nonce=b"\x00" * 12,
            session_key=None,
        )
        bad_crc = b"\xDE\xAD\xBE\xEF"
        encoded = cobs.encode(body + bad_crc)

        await serial._process_packet(encoded)
        assert state.serial_decode_errors >= 1
    finally:
        state.cleanup()


@pytest.mark.asyncio
async def test_process_packet_valid_system_command() -> None:
    """Valid packet for a system command bypasses anti-replay and updates metrics."""
    config = _make_config()
    state = create_runtime_state(config)
    try:
        serial = SerialTransport(config, state, None)
        encoded = _valid_packet(Command.CMD_GET_VERSION_RESP.value)

        # Verify _process_packet completes without error (metrics incremented internally)
        await serial._process_packet(encoded)
        # Success: no exception raised, packet was processed
    finally:
        state.cleanup()


@pytest.mark.asyncio
async def test_process_packet_anti_replay_validation() -> None:
    """Non-system command with synchronized state goes through anti-replay check."""
    config = _make_config()
    state = create_runtime_state(config)
    try:
        serial = SerialTransport(config, state, None)
        state.mark_synchronized()
        state.link_last_nonce_counter = 0

        nonce, _ = generate_nonce_with_counter(0)  # counter=1 embedded

        encoded = _valid_packet(Command.CMD_SET_PIN_MODE.value)
        # Rebuild with correct nonce so anti-replay passes
        from mcubridge.protocol.frame import build_frame

        body = build_frame(
            command_id=Command.CMD_SET_PIN_MODE.value,
            sequence_id=1,
            payload=b"",
            nonce=nonce,
            session_key=None,
        )
        crc_val = crc32(body) & 0xFFFFFFFF
        encoded_nonce = cobs.encode(body + crc_val.to_bytes(4, "little"))

        await serial._process_packet(encoded_nonce)
        assert state.link_last_nonce_counter == 1
    finally:
        state.cleanup()


@pytest.mark.asyncio
async def test_process_packet_with_service_dispatch() -> None:
    """Valid packet dispatches to service.handle_mcu_frame."""
    config = _make_config()
    state = create_runtime_state(config)
    try:
        mock_service = MagicMock()
        mock_service.register_serial_sender = MagicMock()
        mock_service.handle_mcu_frame = AsyncMock()
        serial = SerialTransport(config, state, mock_service)
        encoded = _valid_packet(Command.CMD_GET_VERSION_RESP.value)

        await serial._process_packet(encoded)
        mock_service.handle_mcu_frame.assert_awaited_once()
    finally:
        state.cleanup()


@pytest.mark.asyncio
async def test_process_packet_baudrate_negotiation() -> None:
    """During baudrate negotiation, CMD_SET_BAUDRATE_RESP resolves the future."""
    config = _make_config()
    state = create_runtime_state(config)
    try:
        serial = SerialTransport(config, state, None)
        serial._negotiating = True
        loop = asyncio.get_event_loop()
        fut: asyncio.Future[bool] = loop.create_future()
        serial._negotiation_future = fut

        mock_w = _mock_writer()
        serial.writer = mock_w

        encoded = _valid_packet(Command.CMD_SET_BAUDRATE_RESP.value)

        with patch.object(serial, "_switch_local_baudrate"):
            await serial._process_packet(encoded)

        assert fut.done() and fut.result() is True
    finally:
        state.cleanup()


# ---------------------------------------------------------------------------
# acknowledge()
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_acknowledge_sends_ack() -> None:
    config = _make_config()
    state = create_runtime_state(config)
    try:
        serial = SerialTransport(config, state, None)
        mock_w = _mock_writer()
        serial.writer = mock_w
        state.serial_tx_allowed.set()

        await serial.acknowledge(Command.CMD_GET_VERSION.value, seq_id=1)
        mock_w.write.assert_called_once()
    finally:
        state.cleanup()
