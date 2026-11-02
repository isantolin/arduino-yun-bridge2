"""Surgical unit test suite for transport/serial.py covering edge paths and error branches."""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from typing import Any, cast
from unittest.mock import AsyncMock, MagicMock, PropertyMock

import mcubridge.protocol.mcubridge_pb2 as pb
import pytest
import serial
from pw_hdlc.decode import FrameDecoder
from pw_hdlc.encode import ui_frame
from serial import SerialException
from google.protobuf.message import Message as ProtobufMessage
from pytest_mock import MockerFixture
from mcubridge.config.settings import RuntimeConfig
from mcubridge.protocol import protocol
from mcubridge.protocol.protocol import Command, Status
from mcubridge.security.security import LinkCipher, derive_link_keys
from mcubridge.state.context import RuntimeState, create_runtime_state
from mcubridge.transport.serial import SerialTransport


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


async def test_switch_local_baudrate_failure_raises(mock_config: RuntimeConfig, mock_state: RuntimeState) -> None:
    transport = SerialTransport(mock_config, mock_state, None)
    mock_serial = MagicMock(spec=serial.Serial)
    type(mock_serial).baudrate = PropertyMock(side_effect=AttributeError("No baudrate attr"))
    transport.serial = mock_serial

    switch_local_baudrate: Callable[[int], None] = transport.switch_local_baudrate
    with pytest.raises(RuntimeError, match="UART access failed"):
        switch_local_baudrate(115200)


async def test_toggle_dtr_exception_handled(
    mock_config: RuntimeConfig, mock_state: RuntimeState, monkeypatch: Any
) -> None:
    monkeypatch.setattr("asyncio.sleep", AsyncMock())
    monkeypatch.setattr("time.sleep", MagicMock())
    transport = SerialTransport(mock_config, mock_state, None)
    mock_serial = MagicMock(spec=serial.Serial)
    type(mock_serial).dtr = PropertyMock(side_effect=SerialException("DTR failed"))
    transport.serial = mock_serial

    toggle_dtr: Callable[[], Awaitable[None]] = transport.toggle_dtr
    await toggle_dtr()
    assert transport.serial is mock_serial


async def test_read_loop_data_flow(mock_config: RuntimeConfig, mock_state: RuntimeState) -> None:
    transport = SerialTransport(mock_config, mock_state, None)
    mock_serial = MagicMock(spec=serial.Serial)
    mock_serial.read_until.side_effect = [
        b"~",
        OSError("Done"),
    ]

    read_loop: Callable[..., Awaitable[None]] = transport.read_loop
    await read_loop(mock_serial)
    assert mock_serial.read_until.call_count == 2


async def test_read_loop_generic_exception(mock_config: RuntimeConfig, mock_state: RuntimeState) -> None:
    transport = SerialTransport(mock_config, mock_state, None)
    mock_serial = MagicMock(spec=serial.Serial)
    mock_serial.read_until.side_effect = OSError("Read hardware error")

    read_loop: Callable[..., Awaitable[None]] = transport.read_loop
    await read_loop(mock_serial)
    mock_serial.read_until.assert_called_once_with(protocol.FRAME_DELIMITER, protocol.MAX_SERIAL_FRAME_BYTES)


async def test_feed_data_baudrate_negotiation_response(mock_config: RuntimeConfig, mock_state: RuntimeState) -> None:
    transport = SerialTransport(mock_config, mock_state, None)
    mock_serial = MagicMock(spec=serial.Serial)
    mock_serial.baudrate = 9600
    transport.serial = mock_serial
    nonce = bytes(range(protocol.HANDSHAKE_NONCE_LENGTH))
    host_to_mcu, mcu_to_host = derive_link_keys(mock_config.serial_shared_secret, nonce)
    transport.state.link_cipher = LinkCipher(host_to_mcu, mcu_to_host)
    mcu_cipher = LinkCipher(mcu_to_host, host_to_mcu)
    transport.state.connection_fsm.start_negotiating()
    fut: asyncio.Future[bool] = asyncio.Future()
    transport.negotiation_future = fut
    transport.negotiation_target_baudrate = mock_config.serial_baud

    cleartext = protocol.encode_hdlc_rpc(Command.CMD_SET_BAUDRATE_RESP.value, b"", call_id=1)
    frame = next(FrameDecoder().process(cleartext))
    raw = ui_frame(
        protocol.HDLC_ADDRESS_RPC,
        mcu_cipher.encrypt(protocol.HDLC_ADDRESS_RPC, bytes(frame.data)),
    )
    await transport.feed_data(raw)
    assert fut.done()
    assert fut.result() is True
    assert mock_serial.baudrate == 115200


async def test_correlate_frame_success_status(mock_config: RuntimeConfig, mock_state: RuntimeState) -> None:
    transport = SerialTransport(mock_config, mock_state, None)
    pending = MagicMock()
    pending.command_id = Command.CMD_FILE_WRITE.value
    pending.expected_resp_ids = set()
    pending.success = None
    transport.current_command = pending

    correlate_frame: Callable[..., None] = transport.correlate_frame
    correlate_frame(Status.OK.value, b"ok_payload")

    pending.mark_success.assert_called_once_with(b"ok_payload")


async def test_stop_sets_event_and_closes_serial(mock_config: RuntimeConfig, mock_state: RuntimeState) -> None:
    transport = SerialTransport(mock_config, mock_state, None)
    mock_serial = MagicMock(spec=serial.Serial)
    transport.serial = mock_serial

    await transport.stop()

    stop_event = transport.stop_event
    assert stop_event.is_set()
    mock_serial.close.assert_called_once()


async def test_reset_marks_failure(mock_config: RuntimeConfig, mock_state: RuntimeState) -> None:
    transport = SerialTransport(mock_config, mock_state, None)
    pending = MagicMock()
    transport.current_command = pending

    await transport.reset()

    pending.mark_failure.assert_called_once_with(Status.TIMEOUT.value)
    assert transport.current_command is None


async def test_send_raw_no_serial(mock_config: RuntimeConfig, mock_state: RuntimeState) -> None:
    transport = SerialTransport(mock_config, mock_state, None)
    transport.serial = None
    ok = await transport.send_raw(Command.CMD_GET_VERSION.value, b"")
    assert ok is False


async def test_check_baudrate_fallback_triggers(mock_config: RuntimeConfig, mock_state: RuntimeState) -> None:
    transport = SerialTransport(mock_config, mock_state, None)
    transport.consecutive_crc_errors = mock_config.serial_fallback_threshold - 1

    check_fallback: Callable[[], Awaitable[None]] = transport.check_baudrate_fallback
    await check_fallback()
    assert transport.consecutive_crc_errors == 0


async def test_correlate_frame_failure_status(mock_config: RuntimeConfig, mock_state: RuntimeState) -> None:
    from mcubridge.protocol.structures import PendingCommand

    transport = SerialTransport(mock_config, mock_state, None)
    pending = PendingCommand(
        command_id=Command.CMD_FILE_READ.value, expected_resp_ids=[Command.CMD_FILE_READ_RESP.value]
    )
    transport.current_command = pending

    # Response to request matching
    correlate_frame: Callable[..., None] = transport.correlate_frame
    correlate_frame(Command.CMD_FILE_READ_RESP.value, b"content")
    assert pending.completion.is_set()
    assert pending.success is True
    assert pending.response_payload == b"content"


async def test_logging_not_debug_branches(
    mock_config: RuntimeConfig, mock_state: RuntimeState, mocker: MockerFixture
) -> None:
    transport = SerialTransport(mock_config, mock_state, None)
    nonce = bytes(range(protocol.HANDSHAKE_NONCE_LENGTH))
    host_to_mcu, mcu_to_host = derive_link_keys(mock_config.serial_shared_secret, nonce)
    transport.state.link_cipher = LinkCipher(host_to_mcu, mcu_to_host)
    mcu_cipher = LinkCipher(mcu_to_host, host_to_mcu)
    mock_serial = MagicMock(spec=serial.Serial)
    mock_serial.is_open = True
    mock_serial.write = MagicMock(return_value=10)
    mock_serial.flush = MagicMock()
    transport.serial = mock_serial

    mocker.patch("mcubridge.transport.serial.logger.is_enabled_for", return_value=False)
    # 1. send_raw when logger is not debug
    ok = await transport.send_raw(Command.CMD_GET_VERSION.value, b"")
    assert ok is True

    # 2. feed_data when logger is not debug
    payload = pb.VersionResponse(major=2, minor=8, patch=8)
    cleartext = protocol.encode_hdlc_rpc(Command.CMD_GET_VERSION_RESP.value, payload, call_id=1)
    frame = next(FrameDecoder().process(cleartext))
    encoded = ui_frame(
        protocol.HDLC_ADDRESS_RPC,
        mcu_cipher.encrypt(protocol.HDLC_ADDRESS_RPC, bytes(frame.data)),
    )
    await transport.feed_data(encoded)
    assert mock_state.metrics.serial_bytes_received.value > 0


async def test_feed_data_uninitialized_protobuf(
    mock_config: RuntimeConfig, mock_state: RuntimeState, mocker: MockerFixture
) -> None:
    transport = SerialTransport(mock_config, mock_state, None)
    mock_proto = MagicMock(spec=ProtobufMessage)
    mock_proto.IsInitialized.return_value = False

    fake_spec = protocol.OperationSpec(
        name="fake",
        command_id=0x50,
        service_id=0x1000,
        method_id=0x2000,
        response_type=cast(Any, MagicMock(return_value=mock_proto)),
    )

    mocker.patch.dict(protocol.BY_RPC, {(0x1000, 0x2000): fake_spec})

    encoded = protocol.encode_hdlc_rpc(1, 0x1000, 0x2000, 1, b"dummy_payload")
    initial_errors = mock_state.serial_decode_errors
    await transport.feed_data(encoded)
    assert mock_state.serial_decode_errors == initial_errors + 1


async def test_connect_and_run_negotiation_failure(
    mock_config: RuntimeConfig, mock_state: RuntimeState, mocker: MockerFixture
) -> None:
    mock_config.serial_baud = 115200
    mock_config.serial_safe_baud = 9600
    mock_config.serial_port = "/dev/ttyS0"
    transport = SerialTransport(mock_config, mock_state, None)

    mock_serial_obj = MagicMock(spec=serial.Serial)
    mock_serial_obj.is_open = True
    mock_serial_obj.dtr = True

    mocker.patch("serial.serial_for_url", return_value=mock_serial_obj)
    mocker.patch.object(transport, "toggle_dtr", new=AsyncMock())
    mocker.patch.object(transport, "negotiate_baudrate", new=AsyncMock(return_value=False))
    with pytest.raises(ConnectionError, match="Baudrate negotiation failed"):
        await transport.connect_and_run()


def test_serial_transport_negotiating_state(mock_config: RuntimeConfig, mock_state: RuntimeState) -> None:
    transport = SerialTransport(mock_config, mock_state, None)
    assert not transport.state.connection_fsm.negotiating_baud.is_active
    transport.state.connection_fsm.start_negotiating()
    assert transport.state.connection_fsm.negotiating_baud.is_active
    transport.state.connection_fsm.disconnect()
    assert not transport.state.connection_fsm.negotiating_baud.is_active
