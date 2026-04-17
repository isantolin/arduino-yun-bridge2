from unittest.mock import AsyncMock, MagicMock

import asyncio
import pytest
from cobs import cobs
from mcubridge.config.settings import RuntimeConfig
from mcubridge.protocol.frame import Frame
from mcubridge.protocol.protocol import Command
from mcubridge.services.runtime import BridgeService
from mcubridge.state.context import create_runtime_state
from mcubridge.transport.serial import SerialTransport

from tests._helpers import make_test_config


def _make_config() -> RuntimeConfig:
    return make_test_config(
        serial_port="/dev/ttyATH0",
        mqtt_topic="br",
        allowed_commands=("*",),
        serial_shared_secret=b"secret123",
    )


def test_is_raw_binary_frame_valid_size() -> None:
    pass


@pytest.mark.asyncio
async def test_process_packet_crc_mismatch_reports_crc(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config = _make_config()
    state = create_runtime_state(config)
    try:
        state.mark_transport_connected()
        state.mark_synchronized()
        service = BridgeService(config, state)

        # Use SerialTransport to test async process packet logic
        transport = SerialTransport(config, state, service)
        transport.loop = asyncio.get_running_loop()

        # Create an invalid frame manually (e.g. version mismatch to trigger ValueError in Frame.parse)
        raw = b"\xff" + b"x" * 20
        monkeypatch.setattr(cobs, "decode", lambda _data: raw)  # type: ignore[reportUnknownLambdaType]

        # Manual call to async method
        await transport._async_process_packet(b"\x02encoded")  # type: ignore[reportPrivateUsage]

        assert state.serial_decode_errors == 1
    finally:
        state.cleanup()


@pytest.mark.asyncio
async def test_process_packet_success_dispatches() -> None:
    config = _make_config()
    state = create_runtime_state(config)
    try:
        service = BridgeService(config, state)

        service.handle_mcu_frame = AsyncMock()

        frame_bytes = Frame(
            command_id=Command.CMD_CONSOLE_WRITE.value, sequence_id=0, payload=b"hi"
        ).build()
        encoded = cobs.encode(frame_bytes)
        transport = SerialTransport(config, state, service)

        transport.loop = asyncio.get_running_loop()

        await transport._async_process_packet(encoded)  # type: ignore[reportPrivateUsage]

        service.handle_mcu_frame.assert_awaited_once_with(
            Command.CMD_CONSOLE_WRITE.value, 0, b"hi"
        )
    finally:
        state.cleanup()


@pytest.mark.asyncio
async def test_process_packet_negotiation_ack_switches_local_baudrate() -> None:
    config = _make_config()
    config.serial_baud = 230400
    config.serial_safe_baud = 115200
    state = create_runtime_state(config)
    try:
        service = BridgeService(config, state)

        transport = SerialTransport(config, state, service)
        transport.loop = asyncio.get_running_loop()

        mock_writer = MagicMock(spec=asyncio.StreamWriter)
        mock_writer.is_closing.return_value = False
        serial_port = MagicMock()
        serial_port.baudrate = config.serial_safe_baud
        mock_writer.transport = MagicMock(serial=serial_port)
        transport.writer = mock_writer

        transport._negotiating = True  # type: ignore[reportPrivateUsage]
        transport._negotiation_future = transport.loop.create_future()  # type: ignore[reportPrivateUsage]

        encoded = cobs.encode(
            Frame(
                command_id=Command.CMD_SET_BAUDRATE_RESP.value,
                sequence_id=0,
                payload=b"",
            ).build()
        )
        transport._process_packet(encoded)  # type: ignore[reportPrivateUsage]

        assert await transport._negotiation_future is True  # type: ignore[reportPrivateUsage]
        assert serial_port.baudrate == config.serial_baud
    finally:
        state.cleanup()


@pytest.mark.asyncio
async def test_write_frame_debug_logs_unknown_command(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config = _make_config()
    state = create_runtime_state(config)
    try:
        service = BridgeService(config, state)
        import mcubridge.transport.serial

        transport = SerialTransport(config, state, service)
        mock_writer = MagicMock(spec=asyncio.StreamWriter)
        mock_writer.is_closing.return_value = False
        transport.writer = mock_writer

        monkeypatch.setattr(
            mcubridge.transport.serial.logger,
            "isEnabledFor",
            lambda _lvl: True,  # type: ignore[reportUnknownLambdaType]
        )
        seen: dict[str, str] = {}
        monkeypatch.setattr(
            mcubridge.transport.serial.logger,
            "debug",
            lambda msg, *args: seen.setdefault("msg", msg % args),  # type: ignore[reportUnknownLambdaType]
        )
        monkeypatch.setattr(
            mcubridge.transport.serial.logger,
            "log",
            lambda _lvl, msg, *args: seen.setdefault("msg", msg % args),  # type: ignore[reportUnknownLambdaType]
        )

        ok = await transport._serial_sender(0xFE, b"payload")  # type: ignore[reportPrivateUsage]
        assert ok is True
        assert mock_writer.write.called
        # Check that the command 0xFE is present in the encoded hex string
        assert "fe" in seen.get("msg", "").lower()
    finally:
        state.cleanup()


@pytest.mark.asyncio
async def test_write_frame_returns_false_on_write_error() -> None:
    config = _make_config()
    state = create_runtime_state(config)
    try:
        service = BridgeService(config, state)

        transport = SerialTransport(config, state, service)
        mock_writer = MagicMock(spec=asyncio.StreamWriter)
        mock_writer.is_closing.return_value = False
        mock_writer.write.side_effect = OSError("boom")
        transport.writer = mock_writer

        ok = await transport._serial_sender(Command.CMD_CONSOLE_WRITE.value, b"hi")  # type: ignore[reportPrivateUsage]
        assert ok is False
    finally:
        state.cleanup()


@pytest.mark.asyncio
async def test_process_packet_fallback_triggers_negotiation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config = _make_config()
    config.serial_baud = 115200
    config.serial_safe_baud = 57600
    config.serial_fallback_threshold = 2
    state = create_runtime_state(config)
    try:
        state.mark_transport_connected()
        state.mark_synchronized()
        service = BridgeService(config, state)

        transport = SerialTransport(config, state, service)
        transport.loop = asyncio.get_running_loop()

        # Mock negotiation method
        transport._negotiate_baudrate = AsyncMock(return_value=True)  # type: ignore[reportPrivateUsage]

        # Create an invalid frame manually
        raw = b"\xff" + b"x" * 20
        monkeypatch.setattr(cobs, "decode", lambda _data: raw)  # type: ignore[reportUnknownLambdaType]

        await transport._async_process_packet(b"\x02encoded")  # type: ignore[reportPrivateUsage]
        assert transport._consecutive_crc_errors == 1  # type: ignore[reportPrivateUsage]

        transport._negotiate_baudrate.assert_not_called()  # type: ignore[reportPrivateUsage]

        # Second error (threshold reached)
        await transport._async_process_packet(b"\x02encoded")  # type: ignore[reportPrivateUsage]
        assert transport._consecutive_crc_errors == 0  # type: ignore[reportPrivateUsage]

        transport._negotiate_baudrate.assert_awaited_once_with(57600)  # type: ignore[reportPrivateUsage]
    finally:
        state.cleanup()
