from unittest.mock import AsyncMock, MagicMock

import asyncio
import pytest
from mcubridge.config.settings import RuntimeConfig
from mcubridge.protocol.frame import Frame
from mcubridge.protocol.protocol import Command
from mcubridge.services.runtime import BridgeService
from mcubridge.state.context import create_runtime_state
from mcubridge.transport import serial as serial_fast


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
    assert serial_fast._is_binary_packet(b"\x02\x00\x00\x00\x00") is True
    assert serial_fast._is_binary_packet(bytearray(b"\x02\x00\x00\x00\x00")) is True
    assert serial_fast._is_binary_packet(b"") is False
    # Use actual constant name MAX_SERIAL_FRAME_BYTES
    assert serial_fast._is_binary_packet(b"a" * (serial_fast.MAX_SERIAL_FRAME_BYTES + 1)) is False


@pytest.mark.asyncio
async def test_process_packet_crc_mismatch_reports_crc(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config = _make_config()
    state = create_runtime_state(config)
    state.mark_transport_connected()
    state.mark_synchronized()
    service = BridgeService(config, state)

    # Use SerialTransport to test async process packet logic
    transport = serial_fast.SerialTransport(config, state, service)
    transport.loop = asyncio.get_running_loop()

    # Create an invalid frame manually (e.g. version mismatch to trigger ValueError in Frame.parse)
    raw = b"\xff" + b"x" * 20
    monkeypatch.setattr(serial_fast, "cobs_decode", lambda _data: raw)

    # Manual call to async method
    await transport._async_process_packet(b"\x02encoded")

    assert state.serial_decode_errors == 1


@pytest.mark.asyncio
async def test_process_packet_success_dispatches(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config = _make_config()
    state = create_runtime_state(config)
    service = BridgeService(config, state)

    service.handle_mcu_frame = AsyncMock()

    frame_bytes = Frame.build(Command.CMD_CONSOLE_WRITE.value, b"hi")
    monkeypatch.setattr(serial_fast, "cobs_decode", lambda _data: frame_bytes)

    transport = serial_fast.SerialTransport(config, state, service)
    transport.loop = asyncio.get_running_loop()

    await transport._async_process_packet(b"\x02encoded")

    service.handle_mcu_frame.assert_awaited_once_with(Command.CMD_CONSOLE_WRITE.value, b"hi")


@pytest.mark.asyncio
async def test_write_frame_debug_logs_unknown_command(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config = _make_config()
    state = create_runtime_state(config)
    service = BridgeService(config, state)

    transport = serial_fast.SerialTransport(config, state, service)
    mock_writer = MagicMock(spec=asyncio.StreamWriter)
    mock_writer.is_closing.return_value = False
    transport.writer = mock_writer

    monkeypatch.setattr(serial_fast.logger, "isEnabledFor", lambda _lvl: True)
    seen: dict[str, str] = {}
    monkeypatch.setattr(
        serial_fast.logger,
        "debug",
        lambda msg, *args: seen.setdefault("msg", msg % args),
    )
    monkeypatch.setattr(
        serial_fast.logger,
        "log",
        lambda _lvl, msg, *args: seen.setdefault("msg", msg % args),
    )

    ok = await transport._serial_sender(0xFE, b"payload")
    assert ok is True
    assert mock_writer.write.called
    # Check that the command 0xFE is present in the encoded hex string
    assert "fe" in seen.get("msg", "").lower()

@pytest.mark.asyncio
async def test_write_frame_returns_false_on_write_error() -> None:
    config = _make_config()
    state = create_runtime_state(config)
    service = BridgeService(config, state)

    transport = serial_fast.SerialTransport(config, state, service)
    mock_writer = MagicMock(spec=asyncio.StreamWriter)
    mock_writer.is_closing.return_value = False
    mock_writer.write.side_effect = OSError("boom")
    transport.writer = mock_writer

    ok = await transport._serial_sender(Command.CMD_CONSOLE_WRITE.value, b"hi")
    assert ok is False
