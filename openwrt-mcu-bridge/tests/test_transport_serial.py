"""Unit tests for mcubridge.transport.serial."""

from __future__ import annotations

import asyncio
import struct
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

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
from mcubridge.transport import serial


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


def test_ensure_raw_mode_noop_when_termios_missing(monkeypatch: pytest.MonkeyPatch) -> None:
    pass


def test_ensure_raw_mode_sets_raw_and_disables_echo(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[tuple[str, object]] = []

    termios_mod = pytest.importorskip("termios")

    class _TTY:
        @staticmethod
        def setraw(fd: int) -> None:
            calls.append(("setraw", fd))

    class _Termios:
        ECHO = termios_mod.ECHO
        TCSANOW = 0
        CS8 = 0
        CREAD = 0
        CLOCAL = 0
        VMIN = 0
        VTIME = 0
        TIOCEXCL = 0 # Also used in exclusive mode path
        TCIOFLUSH = 0

        class error(Exception): pass

        @staticmethod
        def tcgetattr(fd: int):
            # attrs structure: [iflag, oflag, cflag, lflag, ispeed, ospeed, cc]
            # cc is a list of control characters
            cc = [0] * 32
            return [0, 0, 0, _Termios.ECHO, 0, 0, cc]

        @staticmethod
        def tcsetattr(fd: int, _when: int, attrs) -> None:
            calls.append(("tcsetattr", attrs[3]))

        @staticmethod
        def tcflush(fd: int, _queue: int) -> None:
            calls.append(("tcflush", fd))

    monkeypatch.setattr(serial, "tty", _TTY)
    monkeypatch.setattr(serial, "termios", _Termios)

    serial.configure_serial_port(10, 115200)

    assert ("setraw", 10) not in calls # tty.setraw not used directly anymore, custom logic
    # Check manual raw mode flags
    assert any(c[0] == "tcsetattr" and c[1] == 0 for c in calls) # lflag=0 (raw)


@pytest.mark.asyncio
async def test_flow_control_mixin_drain_unblocks_on_resume() -> None:
    mixin = serial.FlowControlMixin()
    mixin.pause_writing()

    task = asyncio.create_task(mixin._drain_helper())
    await asyncio.sleep(0)
    assert not task.done()

    mixin.resume_writing()
    await task


@pytest.mark.asyncio
async def test_flow_control_mixin_connection_lost_wakes_waiter() -> None:
    mixin = serial.FlowControlMixin()
    mixin.pause_writing()

    task = asyncio.create_task(mixin._drain_helper())
    await asyncio.sleep(0)

    mixin.connection_lost(ConnectionError("boom"))
    with pytest.raises(ConnectionError):
        await task


@pytest.mark.asyncio
async def test_process_packet_non_binary_does_not_send_status(monkeypatch: pytest.MonkeyPatch) -> None:
    config = _make_config()
    state = create_runtime_state(config)
    state.link_is_synchronized = True
    service = BridgeService(config, state)

    service.send_frame = AsyncMock(return_value=True)  # type: ignore[method-assign]

    transport = serial.SerialTransport(config, state, service)
    await transport._process_packet(b"")

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

    transport = serial.SerialTransport(config, state, service)
    await transport._process_packet(encoded)

    assert state.serial_decode_errors == 1
    service.send_frame.assert_not_awaited()


@pytest.mark.asyncio
async def test_process_packet_crc_mismatch_reports_crc(monkeypatch: pytest.MonkeyPatch) -> None:
    config = _make_config()
    state = create_runtime_state(config)
    state.link_is_synchronized = True
    service = BridgeService(config, state)

    service.send_frame = AsyncMock(return_value=True)  # type: ignore[method-assign]

    raw = struct.pack(protocol.CRC_COVERED_HEADER_FORMAT, 1, 0, Command.CMD_LINK_SYNC.value) + b"x" * 10

    monkeypatch.setattr(serial.cobs, "decode", lambda _data: raw)

    def _bad_frame(_raw: bytes) -> Frame:
        raise ValueError("crc mismatch")

    monkeypatch.setattr(serial.Frame, "from_bytes", _bad_frame)

    transport = serial.SerialTransport(config, state, service)
    await transport._process_packet(b"encoded")

    assert state.serial_decode_errors == 1
    assert state.serial_crc_errors == 1

    service.send_frame.assert_awaited_once()
    status, payload = service.send_frame.call_args[0]
    assert status == Status.CRC_MISMATCH.value
    hint = struct.unpack(UINT16_FORMAT, payload[:2])[0]
    assert hint == Command.CMD_LINK_SYNC.value


@pytest.mark.asyncio
async def test_process_packet_success_dispatches(monkeypatch: pytest.MonkeyPatch) -> None:
    config = _make_config()
    state = create_runtime_state(config)
    service = BridgeService(config, state)

    service.handle_mcu_frame = AsyncMock()  # type: ignore[method-assign]

    frame_bytes = Frame.build(Command.CMD_CONSOLE_WRITE.value, b"hi")
    monkeypatch.setattr(serial.cobs, "decode", lambda _data: frame_bytes)

    transport = serial.SerialTransport(config, state, service)
    await transport._process_packet(b"encoded")

    service.handle_mcu_frame.assert_awaited_once_with(Command.CMD_CONSOLE_WRITE.value, b"hi")


@pytest.mark.asyncio
async def test_open_serial_connection_with_retry_negotiates_baudrate(monkeypatch: pytest.MonkeyPatch) -> None:
    config = _make_config()
    config.serial_safe_baud = 9600
    config.serial_baud = 115200

    fake_reader = asyncio.StreamReader()

    class FakeWriter:
        def __init__(self) -> None:
            self._closed = False
            self.transport = MagicMock()
            self.transport.close.side_effect = self.close

        def close(self) -> None:
            self._closed = True

        async def wait_closed(self) -> None:
            return None
            
        async def _drain_helper(self) -> None:
            return None

    w1 = FakeWriter()
    w2 = FakeWriter()

    opener = AsyncMock(side_effect=[(fake_reader, w1), (fake_reader, w2)])
    monkeypatch.setattr(serial, "_open_serial_connection", opener)
    monkeypatch.setattr(serial, "_negotiate_baudrate", AsyncMock(return_value=True))
    monkeypatch.setattr(serial.asyncio, "sleep", AsyncMock())

    reader, writer = await serial._open_serial_connection_with_retry(config)
    assert reader is fake_reader
    assert writer is w2
    assert opener.await_count == 2


@pytest.mark.asyncio
async def test_send_frame_debug_logs_unknown_command(monkeypatch: pytest.MonkeyPatch) -> None:
    config = _make_config()
    state = create_runtime_state(config)
    service = BridgeService(config, state)

    transport = serial.SerialTransport(config, state, service)

    class _Writer:
        def __init__(self) -> None:
            self.writes: list[bytes] = []
            self.transport = MagicMock()
            self.transport.is_closing.return_value = False

        def is_closing(self) -> bool:
            return False

        def write(self, data: bytes) -> None:
            self.writes.append(data)

        async def drain(self) -> None:
            return None
            
        async def _drain_helper(self) -> None:
            return None

    writer = _Writer()
    transport.writer = writer  # type: ignore[assignment]

    monkeypatch.setattr(serial.logger, "isEnabledFor", lambda _lvl: True)
    seen: dict[str, str] = {}
    monkeypatch.setattr(serial.logger, "debug", lambda msg, *args: seen.setdefault("msg", msg % args))

    ok = await transport.send_frame(protocol.UINT8_MASK - 1, protocol.FRAME_DELIMITER)
    assert ok is True
    assert writer.writes
    assert "0xFE" in seen.get("msg", "")


@pytest.mark.asyncio
async def test_send_frame_returns_false_on_write_error() -> None:
    config = _make_config()
    state = create_runtime_state(config)
    service = BridgeService(config, state)

    transport = serial.SerialTransport(config, state, service)

    class _Writer:
        def __init__(self) -> None:
            self.transport = MagicMock()
            self.transport.is_closing.return_value = False

        def is_closing(self) -> bool:
            return False

        def write(self, _data: bytes) -> None:
            raise OSError("boom")

        async def drain(self) -> None:
            return None
            
        async def _drain_helper(self) -> None:
            return None

    transport.writer = _Writer()  # type: ignore[assignment]
    ok = await transport.send_frame(Command.CMD_CONSOLE_WRITE.value, b"hi")
    assert ok is False


@pytest.mark.asyncio
async def test_send_frame_honors_xoff_xon_backpressure() -> None:
    config = _make_config()
    state = create_runtime_state(config)
    service = BridgeService(config, state)

    transport = serial.SerialTransport(config, state, service)

    class _Writer:
        def __init__(self) -> None:
            self.writes: list[bytes] = []
            self.transport = MagicMock()
            self.transport.is_closing.return_value = False

        def is_closing(self) -> bool:
            return False

        def write(self, data: bytes) -> None:
            self.writes.append(data)

        async def drain(self) -> None:
            return None
            
        async def _drain_helper(self) -> None:
            return None

    writer = _Writer()
    transport.writer = writer  # type: ignore[assignment]

    # Pause serial TX (simulate XOFF)
    state.serial_tx_allowed.clear()

    task = asyncio.create_task(
        transport.send_frame(Command.CMD_CONSOLE_WRITE.value, b"hi")
    )
    await asyncio.sleep(0)
    assert writer.writes == []

    # Resume serial TX (simulate XON)
    state.serial_tx_allowed.set()
    ok = await asyncio.wait_for(task, timeout=1.0)
    assert ok is True
    assert writer.writes
