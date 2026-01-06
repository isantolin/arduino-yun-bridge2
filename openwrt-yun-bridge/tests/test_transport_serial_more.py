"""Additional tests for yunbridge.transport.serial branch coverage."""

from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock
from typing import cast

import pytest

from cobs import cobs

from yunbridge.config.settings import RuntimeConfig
from yunbridge.rpc import protocol
from yunbridge.rpc.frame import Frame
from yunbridge.rpc.protocol import Command
from yunbridge.services.runtime import BridgeService
from yunbridge.services.handshake import SerialHandshakeFatal
from yunbridge.state.context import create_runtime_state
from yunbridge.transport import serial as serial_mod
from yunbridge.transport.termios_serial import SerialException


class _FakeReader:
    def __init__(self, payload: bytes) -> None:
        self._payload = payload

    async def readuntil(self, _delimiter: bytes) -> bytes:
        return self._payload


class _TimeoutReader:
    async def readuntil(self, _delimiter: bytes) -> bytes:
        raise asyncio.TimeoutError


class _FakeWriter:
    def __init__(self) -> None:
        self.writes: list[bytes] = []
        self.closed = False

    def write(self, data: bytes) -> None:
        self.writes.append(data)

    async def drain(self) -> None:
        return None

    def close(self) -> None:
        self.closed = True

    async def wait_closed(self) -> None:
        return None

    def is_closing(self) -> bool:
        return self.closed


@pytest.mark.asyncio
async def test_negotiate_baudrate_success() -> None:
    response_frame = Frame.build(Command.CMD_SET_BAUDRATE_RESP.value, b"")
    response = cobs.encode(response_frame) + protocol.FRAME_DELIMITER

    reader = _FakeReader(response)
    writer = _FakeWriter()

    ok = await serial_mod._negotiate_baudrate(reader, writer, 115200)

    assert ok is True
    assert writer.writes


@pytest.mark.asyncio
async def test_negotiate_baudrate_unexpected_response() -> None:
    response_frame = Frame.build(Command.CMD_GET_VERSION_RESP.value, b"")
    response = cobs.encode(response_frame) + protocol.FRAME_DELIMITER

    reader = _FakeReader(response)
    writer = _FakeWriter()

    ok = await serial_mod._negotiate_baudrate(reader, writer, 115200)

    assert ok is False


@pytest.mark.asyncio
async def test_negotiate_baudrate_timeout_returns_false() -> None:
    reader = _TimeoutReader()
    writer = _FakeWriter()

    ok = await serial_mod._negotiate_baudrate(reader, writer, 115200)

    assert ok is False


def test_open_serial_hardware_closes_on_failure() -> None:
    closed: dict[str, bool] = {"closed": False}

    class _StubSerial:
        fd = None
        is_open = True

        def open(self) -> None:
            raise OSError("open failed")

        def close(self) -> None:
            closed["closed"] = True

    with pytest.raises(OSError):
        serial_mod._open_serial_hardware(_StubSerial(), "loop://")

    assert closed["closed"] is True


@pytest.mark.asyncio
async def test_open_serial_connection_with_retry_no_negotiation(monkeypatch: pytest.MonkeyPatch) -> None:
    config = RuntimeConfig(
        serial_port="loop://",
        serial_baud=115200,
        serial_safe_baud=0,
        mqtt_host="localhost",
        mqtt_port=1883,
        mqtt_user=None,
        mqtt_pass=None,
        mqtt_tls=False,
        mqtt_cafile=None,
        mqtt_certfile=None,
        mqtt_keyfile=None,
        mqtt_topic="br",
        allowed_commands=(),
        file_system_root="/tmp",
        process_timeout=1,
        reconnect_delay=1,
        status_interval=1,
        debug_logging=False,
        console_queue_limit_bytes=64,
        mailbox_queue_limit=2,
        mailbox_queue_bytes_limit=32,
        serial_shared_secret=b"testshared",
    )

    fake_reader = asyncio.StreamReader()
    writer = _FakeWriter()

    opener = AsyncMock(return_value=(fake_reader, writer))
    monkeypatch.setattr(serial_mod, "OPEN_SERIAL_CONNECTION", opener)

    reader, got_writer = await serial_mod._open_serial_connection_with_retry(config)

    assert reader is fake_reader
    assert got_writer is writer
    opener.assert_awaited_once()
    assert opener.call_args.kwargs.get("baudrate") == 115200


@pytest.mark.asyncio
async def test_open_serial_connection_with_retry_negotiation_failure(monkeypatch: pytest.MonkeyPatch) -> None:
    config = RuntimeConfig(
        serial_port="loop://",
        serial_baud=115200,
        serial_safe_baud=9600,
        mqtt_host="localhost",
        mqtt_port=1883,
        mqtt_user=None,
        mqtt_pass=None,
        mqtt_tls=False,
        mqtt_cafile=None,
        mqtt_certfile=None,
        mqtt_keyfile=None,
        mqtt_topic="br",
        allowed_commands=(),
        file_system_root="/tmp",
        process_timeout=1,
        reconnect_delay=1,
        status_interval=1,
        debug_logging=False,
        console_queue_limit_bytes=64,
        mailbox_queue_limit=2,
        mailbox_queue_bytes_limit=32,
        serial_shared_secret=b"testshared",
    )

    fake_reader = asyncio.StreamReader()
    writer = _FakeWriter()

    opener = AsyncMock(return_value=(fake_reader, writer))
    monkeypatch.setattr(serial_mod, "OPEN_SERIAL_CONNECTION", opener)
    monkeypatch.setattr(serial_mod, "_negotiate_baudrate", AsyncMock(return_value=False))

    reader, got_writer = await serial_mod._open_serial_connection_with_retry(config)

    assert reader is fake_reader
    assert got_writer is writer
    opener.assert_awaited_once()


@pytest.mark.asyncio
async def test_open_serial_connection_with_retry_raises_unexpected_exception_group(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config = RuntimeConfig(
        serial_port="loop://",
        serial_baud=115200,
        serial_safe_baud=115200,
        mqtt_host="localhost",
        mqtt_port=1883,
        mqtt_user=None,
        mqtt_pass=None,
        mqtt_tls=False,
        mqtt_cafile=None,
        mqtt_certfile=None,
        mqtt_keyfile=None,
        mqtt_topic="br",
        allowed_commands=(),
        file_system_root="/tmp",
        process_timeout=1,
        reconnect_delay=1,
        status_interval=1,
        debug_logging=False,
        console_queue_limit_bytes=64,
        mailbox_queue_limit=2,
        mailbox_queue_bytes_limit=32,
        serial_shared_secret=b"testshared",
    )

    exc = ExceptionGroup("boom", [SerialException("serial"), ValueError("bad")])

    opener = AsyncMock(side_effect=exc)
    monkeypatch.setattr(serial_mod, "OPEN_SERIAL_CONNECTION", opener)

    with pytest.raises(ExceptionGroup):
        await serial_mod._open_serial_connection_with_retry(config)


@pytest.mark.asyncio
async def test_read_loop_packet_too_large_sends_malformed(monkeypatch: pytest.MonkeyPatch) -> None:
    config = RuntimeConfig(
        serial_port="/dev/null",
        serial_baud=protocol.DEFAULT_BAUDRATE,
        serial_safe_baud=protocol.DEFAULT_SAFE_BAUDRATE,
        mqtt_host="localhost",
        mqtt_port=1883,
        mqtt_user=None,
        mqtt_pass=None,
        mqtt_tls=False,
        mqtt_cafile=None,
        mqtt_certfile=None,
        mqtt_keyfile=None,
        mqtt_topic="br",
        allowed_commands=(),
        file_system_root="/tmp",
        process_timeout=1,
        reconnect_delay=1,
        status_interval=1,
        debug_logging=False,
        console_queue_limit_bytes=64,
        mailbox_queue_limit=2,
        mailbox_queue_bytes_limit=32,
        serial_shared_secret=b"testshared",
    )

    state = create_runtime_state(config)
    service = BridgeService(config, state)
    service.send_frame = AsyncMock(return_value=True)  # type: ignore[method-assign]

    transport = serial_mod.SerialTransport(config, state, service)

    data = bytearray(b"A" * (serial_mod.MAX_SERIAL_PACKET_BYTES + 1))
    data.extend(protocol.FRAME_DELIMITER)

    class _ByteReader:
        def __init__(self, buffer: bytes) -> None:
            self._items = [bytes([b]) for b in buffer]

        async def read(self, _n: int) -> bytes:
            if not self._items:
                return b""
            return self._items.pop(0)

    transport.reader = cast(asyncio.StreamReader, _ByteReader(bytes(data)))

    await transport._read_loop()

    assert state.serial_decode_errors >= 1
    # Before link sync we avoid emitting MALFORMED status frames to reduce
    # handshake/protocol desync. The decode error is still recorded.
    assert service.send_frame.await_count == 0


@pytest.mark.asyncio
async def test_serial_transport_run_stops_on_handshake_fatal(monkeypatch: pytest.MonkeyPatch) -> None:
    config = RuntimeConfig(
        serial_port="/dev/null",
        serial_baud=protocol.DEFAULT_BAUDRATE,
        serial_safe_baud=protocol.DEFAULT_SAFE_BAUDRATE,
        mqtt_host="localhost",
        mqtt_port=1883,
        mqtt_user=None,
        mqtt_pass=None,
        mqtt_tls=False,
        mqtt_cafile=None,
        mqtt_certfile=None,
        mqtt_keyfile=None,
        mqtt_topic="br",
        allowed_commands=(),
        file_system_root="/tmp",
        process_timeout=1,
        reconnect_delay=1,
        status_interval=1,
        debug_logging=False,
        console_queue_limit_bytes=64,
        mailbox_queue_limit=2,
        mailbox_queue_bytes_limit=32,
        serial_shared_secret=b"testshared",
    )

    state = create_runtime_state(config)
    service = BridgeService(config, state)

    writer = _FakeWriter()
    monkeypatch.setattr(
        serial_mod,
        "_open_serial_connection_with_retry",
        AsyncMock(return_value=(asyncio.StreamReader(), writer)),
    )

    transport = serial_mod.SerialTransport(config, state, service)
    monkeypatch.setattr(transport, "_read_loop", AsyncMock(return_value=None))
    monkeypatch.setattr(transport, "_disconnect", AsyncMock(return_value=None))

    monkeypatch.setattr(service, "on_serial_connected", AsyncMock(side_effect=SerialHandshakeFatal("fatal")))

    sleep_spy = AsyncMock()
    monkeypatch.setattr(serial_mod.asyncio, "sleep", sleep_spy)

    with pytest.raises(SerialHandshakeFatal):
        await transport.run()

    sleep_spy.assert_not_awaited()
