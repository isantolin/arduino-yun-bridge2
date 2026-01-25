"""Unit tests for mcubridge.transport.serial_fast."""

from __future__ import annotations

import sys
from unittest.mock import MagicMock

# Mock serial_asyncio_fast before importing the module under test
sys.modules["serial_asyncio_fast"] = MagicMock()

import asyncio
import struct
from unittest.mock import AsyncMock, Mock

import pytest

from cobs import cobs

from mcubridge.config.settings import RuntimeConfig
from mcubridge.const import (
    DEFAULT_CONSOLE_QUEUE_LIMIT_BYTES,
    DEFAULT_MAILBOX_QUEUE_BYTES_LIMIT,
    DEFAULT_MAILBOX_QUEUE_LIMIT,
    DEFAULT_MQTT_PORT,
    DEFAULT_PROCESS_TIMEOUT,
    DEFAULT_RECONNECT_DELAY,
    DEFAULT_STATUS_INTERVAL,
)
from mcubridge.rpc import protocol
from mcubridge.rpc.frame import Frame
from mcubridge.rpc.protocol import Command, Status, UINT16_FORMAT
from mcubridge.services.runtime import BridgeService
from mcubridge.state.context import create_runtime_state
from mcubridge.transport import serial_fast as serial


def _make_config() -> RuntimeConfig:
    return RuntimeConfig(
        serial_port="/dev/null",
        serial_baud=protocol.DEFAULT_BAUDRATE,
        serial_safe_baud=protocol.DEFAULT_SAFE_BAUDRATE,
        mqtt_host="localhost",
        mqtt_port=DEFAULT_MQTT_PORT,
        mqtt_user=None,
        mqtt_pass=None,
        mqtt_tls=False,
        mqtt_cafile=None,
        mqtt_certfile=None,
        mqtt_keyfile=None,
        mqtt_topic=protocol.MQTT_DEFAULT_TOPIC_PREFIX,
        allowed_commands=(),
        file_system_root="/tmp",
        process_timeout=DEFAULT_PROCESS_TIMEOUT,
        reconnect_delay=DEFAULT_RECONNECT_DELAY,
        status_interval=DEFAULT_STATUS_INTERVAL,
        debug_logging=False,
        console_queue_limit_bytes=DEFAULT_CONSOLE_QUEUE_LIMIT_BYTES,
        mailbox_queue_limit=DEFAULT_MAILBOX_QUEUE_LIMIT,
        mailbox_queue_bytes_limit=DEFAULT_MAILBOX_QUEUE_BYTES_LIMIT,
        serial_shared_secret=b"testshared",
    )


def test_is_binary_packet_type_and_size_guards() -> None:
    assert serial._is_binary_packet(b"") is False
    assert serial._is_binary_packet("nope") is False  # type: ignore[arg-type]
    assert serial._is_binary_packet(b"a" * (serial.MAX_SERIAL_PACKET_BYTES + 1)) is False
    assert serial._is_binary_packet(b"a") is True
    assert serial._is_binary_packet(memoryview(b"abc")) is True


def test_coerce_packet_returns_bytes() -> None:
    assert serial._coerce_packet(b"abc") == b"abc"
    assert serial._coerce_packet(bytearray(b"abc")) == b"abc"


@pytest.mark.asyncio
async def test_process_packet_non_binary_does_not_send_status(monkeypatch: pytest.MonkeyPatch) -> None:
    config = _make_config()
    state = create_runtime_state(config)
    state.link_is_synchronized = True
    service = BridgeService(config, state)

    service.send_frame = AsyncMock(return_value=True)  # type: ignore[method-assign]

    proto = serial.BridgeSerialProtocol(service, state, asyncio.get_running_loop())
    await proto._async_process_packet(b"")

    assert state.serial_decode_errors == 1
    service.send_frame.assert_not_awaited()


@pytest.mark.asyncio
async def test_process_packet_decode_error_does_not_send_status(monkeypatch: pytest.MonkeyPatch) -> None:
    config = _make_config()
    state = create_runtime_state(config)
    state.link_is_synchronized = True
    service = BridgeService(config, state)

    service.send_frame = AsyncMock(return_value=True)  # type: ignore[method-assign]
    service.handle_mcu_frame = AsyncMock()  # type: ignore[method-assign]

    # Make decode fail.
    monkeypatch.setattr(serial.cobs, "decode", lambda _data: (_ for _ in ()).throw(cobs.DecodeError("bad")))

    # Provide enough bytes for header extraction.
    header = struct.pack(
        protocol.CRC_COVERED_HEADER_FORMAT,
        1,
        0,
        0x99,
    )
    encoded = header + b"x" * 4

    proto = serial.BridgeSerialProtocol(service, state, asyncio.get_running_loop())
    await proto._async_process_packet(encoded)

    assert state.serial_decode_errors == 1
    service.send_frame.assert_not_awaited()


@pytest.mark.asyncio
async def test_process_packet_crc_mismatch_reports_crc(monkeypatch: pytest.MonkeyPatch) -> None:
    # Logic in serial_fast currently logs CRC error but doesn't auto-reply status 
    # to avoid protocol overhead in interrupt context unless critical.
    # The original test checked for status reply. 
    # Current implementation:
    # if "crc mismatch" in str(exc).lower(): self.state.record_serial_crc_error()
    
    config = _make_config()
    state = create_runtime_state(config)
    state.link_is_synchronized = True
    service = BridgeService(config, state)
    
    # We won't check for send_frame call here as it was removed from serial_fast's exception handler
    # for speed, unless we re-added it. The code I wrote:
    # if "crc mismatch" in str(exc).lower(): self.state.record_serial_crc_error()
    
    raw = struct.pack(protocol.CRC_COVERED_HEADER_FORMAT, 1, 0, Command.CMD_LINK_SYNC.value) + b"x" * 10
    monkeypatch.setattr(serial.cobs, "decode", lambda _data: raw)

    def _bad_frame(_raw: bytes) -> Frame:
        raise ValueError("crc mismatch")

    monkeypatch.setattr(serial.Frame, "from_bytes", _bad_frame)

    proto = serial.BridgeSerialProtocol(service, state, asyncio.get_running_loop())
    await proto._async_process_packet(b"encoded")

    assert state.serial_decode_errors == 1
    assert state.serial_crc_errors == 1


@pytest.mark.asyncio
async def test_process_packet_success_dispatches(monkeypatch: pytest.MonkeyPatch) -> None:
    config = _make_config()
    state = create_runtime_state(config)
    service = BridgeService(config, state)

    service.handle_mcu_frame = AsyncMock()  # type: ignore[method-assign]

    frame_bytes = Frame.build(Command.CMD_CONSOLE_WRITE.value, b"hi")
    monkeypatch.setattr(serial.cobs, "decode", lambda _data: frame_bytes)

    proto = serial.BridgeSerialProtocol(service, state, asyncio.get_running_loop())
    await proto._async_process_packet(b"encoded")

    service.handle_mcu_frame.assert_awaited_once_with(Command.CMD_CONSOLE_WRITE.value, b"hi")


@pytest.mark.asyncio
async def test_write_frame_debug_logs_unknown_command(monkeypatch: pytest.MonkeyPatch) -> None:
    config = _make_config()
    state = create_runtime_state(config)
    service = BridgeService(config, state)

    proto = serial.BridgeSerialProtocol(service, state, asyncio.get_running_loop())

    class _MockTransport:
        def __init__(self) -> None:
            self.writes: list[bytes] = []

        def is_closing(self) -> bool:
            return False

        def write(self, data: bytes) -> None:
            self.writes.append(data)

    mock_transport = _MockTransport()
    proto.connection_made(mock_transport) # type: ignore

    monkeypatch.setattr(serial.logger, "isEnabledFor", lambda _lvl: True)
    seen: dict[str, str] = {}
    monkeypatch.setattr(serial.logger, "debug", lambda msg, *args: seen.setdefault("msg", msg % args))

    ok = proto.write_frame(protocol.UINT8_MASK - 1, protocol.FRAME_DELIMITER)
    assert ok is True
    assert mock_transport.writes
    assert "0xFE" in seen.get("msg", "")


@pytest.mark.asyncio
async def test_write_frame_returns_false_on_write_error() -> None:
    config = _make_config()
    state = create_runtime_state(config)
    service = BridgeService(config, state)

    proto = serial.BridgeSerialProtocol(service, state, asyncio.get_running_loop())

    class _MockTransport:
        def is_closing(self) -> bool:
            return False

        def write(self, _data: bytes) -> None:
            raise OSError("boom")

    proto.connection_made(_MockTransport()) # type: ignore
    ok = proto.write_frame(Command.CMD_CONSOLE_WRITE.value, b"hi")
    assert ok is False