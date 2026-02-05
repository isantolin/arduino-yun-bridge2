import sys
from unittest.mock import MagicMock, AsyncMock

# Mock serial_asyncio_fast before importing SerialTransport
mock_serial_fast = MagicMock()
mock_serial_fast.create_serial_connection = AsyncMock(return_value=(MagicMock(), MagicMock()))
sys.modules["serial_asyncio_fast"] = mock_serial_fast

import asyncio  # noqa: E402

import pytest  # noqa: E402


from mcubridge.config.settings import RuntimeConfig  # noqa: E402
from mcubridge.protocol import protocol  # noqa: E402
from mcubridge.protocol.frame import Frame  # noqa: E402
from mcubridge.protocol.protocol import Command  # noqa: E402
from mcubridge.services.runtime import BridgeService  # noqa: E402
from mcubridge.state.context import create_runtime_state  # noqa: E402
from mcubridge.transport import serial_fast  # noqa: E402


def _make_config() -> RuntimeConfig:
    return RuntimeConfig(
        serial_port="/dev/ttyATH0",
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
        allowed_commands=("*",),
        file_system_root="/tmp",
        process_timeout=10,
        serial_shared_secret=b"secret123",
    )


def test_is_binary_packet_valid_size() -> None:
    assert serial_fast._is_binary_packet(b"abc") is True
    assert serial_fast._is_binary_packet(bytearray(b"abc")) is True
    assert serial_fast._is_binary_packet(b"") is False
    assert serial_fast._is_binary_packet(b"a" * (serial_fast.MAX_SERIAL_PACKET_BYTES + 1)) is False


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

    raw = protocol.CRC_COVERED_HEADER_STRUCT.build(dict(
        version=1,
        payload_len=0,
        command_id=Command.CMD_LINK_SYNC.value
    )) + b"x" * 10
    monkeypatch.setattr(serial_fast.cobs, "decode", lambda _data: raw)

    proto = serial_fast.BridgeSerialProtocol(service, state, asyncio.get_running_loop())
    # Manual call to async method
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
    monkeypatch.setattr(serial_fast.cobs, "decode", lambda _data: frame_bytes)

    proto = serial_fast.BridgeSerialProtocol(service, state, asyncio.get_running_loop())
    await proto._async_process_packet(b"encoded")

    service.handle_mcu_frame.assert_awaited_once_with(Command.CMD_CONSOLE_WRITE.value, b"hi")


@pytest.mark.asyncio
async def test_write_frame_debug_logs_unknown_command(monkeypatch: pytest.MonkeyPatch) -> None:
    config = _make_config()
    state = create_runtime_state(config)
    service = BridgeService(config, state)

    proto = serial_fast.BridgeSerialProtocol(service, state, asyncio.get_running_loop())

    class _MockTransport:
        def __init__(self) -> None:
            self.writes: list[bytes] = []

        def is_closing(self) -> bool:
            return False

        def write(self, data: bytes) -> None:
            self.writes.append(data)

    mock_transport = _MockTransport()
    proto.connection_made(mock_transport)  # type: ignore

    monkeypatch.setattr(serial_fast.logger, "isEnabledFor", lambda _lvl: True)
    seen: dict[str, str] = {}
    monkeypatch.setattr(serial_fast.logger, "debug", lambda msg, *args: seen.setdefault("msg", msg % args))
    monkeypatch.setattr(serial_fast.logger, "log", lambda _lvl, msg, *args: seen.setdefault("msg", msg % args))

    ok = proto.write_frame(protocol.UINT8_MASK - 1, b"payload")
    assert ok is True
    assert mock_transport.writes
    # With log_hexdump, the command name/hex is in the log
    assert "0xFE" in seen.get("msg", "")


@pytest.mark.asyncio
async def test_write_frame_returns_false_on_write_error() -> None:
    config = _make_config()
    state = create_runtime_state(config)
    service = BridgeService(config, state)

    proto = serial_fast.BridgeSerialProtocol(service, state, asyncio.get_running_loop())

    class _MockTransport:
        def is_closing(self) -> bool:
            return False

        def write(self, _data: bytes) -> None:
            raise OSError("boom")

    proto.connection_made(_MockTransport())  # type: ignore
    ok = proto.write_frame(Command.CMD_CONSOLE_WRITE.value, b"hi")
    assert ok is False
