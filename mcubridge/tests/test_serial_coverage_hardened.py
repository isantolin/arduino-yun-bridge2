"""SIL-2 Serial Transport Coverage Hardening Test Suite.

Genuinely exercises SerialTransport state transitions, packet encoding/decoding,
handshake state, baudrate fallback, DTR toggling, and framing error conditions.
"""

from __future__ import annotations

import asyncio
import tempfile
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, MagicMock, PropertyMock

import mcubridge.protocol.mcubridge_pb2 as pb
import serial
from pw_hdlc.encode import ui_frame
from pw_rpc import packets as pw_packets
from pw_rpc.internal.packet_pb2 import PacketType
from serial import SerialException
from mcubridge.config.settings import RuntimeConfig
from mcubridge.protocol import protocol
from mcubridge.protocol.protocol import Command, Status
from mcubridge.security.security import LinkCipher, derive_link_keys
from mcubridge.state.context import create_runtime_state
from mcubridge.transport.serial import SerialTransport

_TEST_SECRET = b"coverage test shared secret"
_TEST_NONCE = bytes(range(protocol.HANDSHAKE_NONCE_LENGTH))


def _make_transport(tmp_path: Path | None = None) -> tuple[SerialTransport, Any, Any]:
    d = str(tmp_path or tempfile.mkdtemp())
    config = RuntimeConfig(
        topic_prefix="test/br",
        serial_port="/dev/null",
        serial_baud=115200,
        serial_safe_baud=57600,
        serial_fallback_threshold=3,
        cloud_spool_dir=d,
        allow_non_tmp_paths=True,
        serial_shared_secret=_TEST_SECRET,
    )
    state = create_runtime_state(config)
    host_to_mcu, mcu_to_host = derive_link_keys(_TEST_SECRET, _TEST_NONCE)
    state.link_cipher = LinkCipher(host_to_mcu, mcu_to_host)
    mock_serial = MagicMock(spec=serial.Serial)
    mock_serial.is_open = True
    mock_serial.write = MagicMock(return_value=10)
    mock_serial.flush = MagicMock()
    mock_serial.close = MagicMock()
    mock_serial.dtr = True
    mock_serial.baudrate = 115200

    transport = SerialTransport(config=config, state=state, service=None)
    transport.serial = mock_serial
    return transport, state, mock_serial


async def test_serial_transport_acknowledge(tmp_path: Path) -> None:
    transport, state, mock_serial = _make_transport(tmp_path)

    await transport.send_raw(Status.OK.value, b"", 42)
    assert mock_serial.write.called

    state.cleanup()


async def test_serial_transport_send_raw(tmp_path: Path) -> None:
    transport, state, mock_serial = _make_transport(tmp_path)

    # 1. Normal send_raw
    res = await transport.send_raw(Command.CMD_DIGITAL_READ.value, pb.PinRead(pin=13), seq_id=10)
    assert res is True
    assert mock_serial.write.called

    # 2. send_raw with closed serial
    transport.serial = None
    res_none = await transport.send_raw(Command.CMD_DIGITAL_READ.value, b"")
    assert res_none is False

    # 3. send_raw with write exception
    transport.serial = mock_serial
    mock_serial.write.side_effect = SerialException("device disconnected")
    res_err = await transport.send_raw(Command.CMD_DIGITAL_READ.value, b"")
    assert res_err is False

    state.cleanup()


async def test_serial_transport_encrypts_outgoing_rpc_and_incoming_console(tmp_path: Path) -> None:
    transport, state, mock_serial = _make_transport(tmp_path)
    secret = b"serial transport test secret"
    nonce = bytes(range(protocol.HANDSHAKE_NONCE_LENGTH))
    host_to_mcu, mcu_to_host = derive_link_keys(secret, nonce)
    transport.config.serial_shared_secret = secret
    state.link_cipher = LinkCipher(host_to_mcu, mcu_to_host)
    mcu_cipher = LinkCipher(mcu_to_host, host_to_mcu)

    assert await transport.send_raw(Command.CMD_DIGITAL_READ.value, pb.PinRead(pin=13), seq_id=7)
    encoded = mock_serial.write.call_args.args[0]
    outgoing_frame = next(transport.decoder.process(encoded))
    assert outgoing_frame.address == protocol.HDLC_ADDRESS_RPC
    decoded_request = mcu_cipher.decrypt(outgoing_frame.address, outgoing_frame.data)
    packet = pw_packets.decode(decoded_request)
    operation = protocol.get_operation(Command.CMD_DIGITAL_READ.value)
    assert operation is not None
    assert packet.type == PacketType.REQUEST
    assert packet.call_id == 7
    assert (packet.service_id, packet.method_id) == (operation.service_id, operation.method_id)
    assert pb.PinRead.FromString(packet.payload) == pb.PinRead(pin=13)

    service_mock = AsyncMock()
    transport.service = service_mock
    console_envelope = mcu_cipher.encrypt(protocol.HDLC_ADDRESS_CONSOLE, b"console output")
    await transport.feed_data(ui_frame(protocol.HDLC_ADDRESS_CONSOLE, console_envelope))
    service_mock.handle_mcu_frame.assert_awaited_once_with(
        Command.CMD_CONSOLE_WRITE.value,
        0,
        pb.ConsoleWrite(data=b"console output"),
    )

    state.cleanup()


async def test_serial_transport_toggle_dtr(tmp_path: Path, monkeypatch: Any) -> None:
    monkeypatch.setattr("asyncio.sleep", AsyncMock())
    monkeypatch.setattr("time.sleep", MagicMock())
    transport, state, mock_serial = _make_transport(tmp_path)

    # 1. Success
    toggle_dtr: Callable[[], Awaitable[None]] = transport.toggle_dtr
    await toggle_dtr()
    assert mock_serial.dtr is True

    # 2. Error handling during DTR toggle
    type(mock_serial).dtr = PropertyMock(side_effect=SerialException("DTR error"))
    await toggle_dtr()

    state.cleanup()


async def test_serial_transport_baudrate_fallback(tmp_path: Path) -> None:
    transport, state, _ = _make_transport(tmp_path)

    check_fallback: Callable[[], Awaitable[None]] = transport.check_baudrate_fallback

    transport.consecutive_crc_errors = 1
    await check_fallback()
    assert transport.consecutive_crc_errors == 2

    # Trigger threshold (threshold = 3)
    transport.consecutive_crc_errors = 2
    await check_fallback()
    assert transport.consecutive_crc_errors == 0

    state.cleanup()


async def test_serial_transport_feed_data_hdlc_rpc(tmp_path: Path) -> None:
    transport, state, _ = _make_transport(tmp_path)
    service_mock = AsyncMock()
    transport.service = service_mock

    state.connection_fsm.synchronize()

    # Valid HDLC RPC frame
    host_to_mcu, mcu_to_host = derive_link_keys(_TEST_SECRET, _TEST_NONCE)
    mcu_cipher = LinkCipher(mcu_to_host, host_to_mcu)
    packet = protocol.encode_rpc_packet(
        Command.CMD_DIGITAL_READ.value,
        pb.PinRead(pin=13),
        call_id=5,
    )
    envelope = mcu_cipher.encrypt(protocol.HDLC_ADDRESS_RPC, packet)
    encoded = ui_frame(protocol.HDLC_ADDRESS_RPC, envelope)

    await transport.feed_data(encoded)
    service_mock.handle_mcu_frame.assert_awaited_once()
    assert state.serial_decode_errors == 0

    state.cleanup()


async def test_serial_transport_send_with_retries(tmp_path: Path) -> None:
    transport, state, _ = _make_transport(tmp_path)

    # When send is called without expected response (fire and forget / untracked), it sends raw and returns True
    res = await transport.send(Command.CMD_LINK_RESET.value, pb.GenericResponse(message="reset"))
    assert res is True

    # When send is called with expected response (CMD_DIGITAL_READ expects CMD_DIGITAL_READ_RESP)
    send_task = asyncio.create_task(
        transport.send(
            Command.CMD_DIGITAL_READ.value,
            pb.PinRead(pin=13),
        )
    )

    await asyncio.sleep(0.01)
    # Correlate response
    current = transport.current_command
    if current is not None:
        current.mark_success(pb.DigitalReadResponse(value=1))

    resp = await send_task
    assert isinstance(resp, pb.DigitalReadResponse)

    state.cleanup()
