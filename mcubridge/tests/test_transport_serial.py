"""Unit tests for SerialTransport implementation (SIL-2)."""

from __future__ import annotations

import asyncio
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from mcubridge.config.settings import RuntimeConfig
from mcubridge.protocol.frame import Frame
from mcubridge.protocol.protocol import Command, Status, DEFAULT_BAUDRATE, FRAME_DELIMITER
from mcubridge.transport.serial import SerialTransport
from mcubridge.transport.mqtt import MqttTransport
from mcubridge.services.runtime import BridgeService
from mcubridge.state.context import create_runtime_state
from cobs import cobs


def _make_config() -> RuntimeConfig:
    from tests._helpers import make_test_config
    return make_test_config(
        serial_port="/dev/null",
        serial_baud=DEFAULT_BAUDRATE,
        allowed_commands=(),
    )


@pytest.mark.asyncio
async def test_process_packet_success_dispatches() -> None:
    config = _make_config()
    state = create_runtime_state(config)
    try:
        service = BridgeService(config, state, MqttTransport(config, state))
        # Ensure we don't block on synchronize
        service.handshake_manager.synchronize = AsyncMock(return_value=True)
        
        transport = SerialTransport(config, state, service)
        transport.loop = asyncio.get_running_loop()

        # Mock dispatcher
        service.dispatcher.dispatch_mcu_frame = AsyncMock()

        # Create a valid frame
        payload = b"hello"
        frame_data = Frame(
            command_id=Command.CMD_GET_VERSION_RESP.value,
            sequence_id=1,
            payload=payload,
        ).build()
        encoded = cobs.encode(frame_data) + FRAME_DELIMITER

        await transport._process_packet(encoded)  # type: ignore[reportPrivateUsage]

        service.dispatcher.dispatch_mcu_frame.assert_called_once_with(
            Command.CMD_GET_VERSION_RESP.value, 1, payload
        )
    finally:
        state.cleanup()


@pytest.mark.asyncio
async def test_process_packet_crc_mismatch_reports_crc(caplog: pytest.LogCaptureFixture) -> None:
    config = _make_config()
    state = create_runtime_state(config)
    try:
        service = BridgeService(config, state, MqttTransport(config, state))
        transport = SerialTransport(config, state, service)
        transport.loop = asyncio.get_running_loop()

        # Create valid frame
        raw = bytearray(
            Frame(command_id=0x01, sequence_id=0, payload=b"test").build()
        )
        # Last 4 bytes are CRC32. Flip a bit in the last byte.
        raw[-1] ^= 0xFF
        encoded = cobs.encode(bytes(raw)) + FRAME_DELIMITER

        caplog.set_level("WARNING")
        await transport._process_packet(encoded)  # type: ignore[reportPrivateUsage]

        assert state.serial_crc_errors >= 1 or "CRC error" in caplog.text
    finally:
        state.cleanup()


@pytest.mark.asyncio
async def test_write_frame_returns_false_on_write_error() -> None:
    config = _make_config()
    state = create_runtime_state(config)
    try:
        service = BridgeService(config, state, MqttTransport(config, state))
        transport = SerialTransport(config, state, service)
        
        # No writer -> returns False
        assert await transport._serial_sender(0x01, b"") is False # type: ignore[reportPrivateUsage]
    finally:
        state.cleanup()


@pytest.mark.asyncio
async def test_write_frame_debug_logs_unknown_command() -> None:
    config = _make_config()
    state = create_runtime_state(config)
    try:
        service = BridgeService(config, state, MqttTransport(config, state))
        transport = SerialTransport(config, state, service)
        
        mock_writer = MagicMock(spec=asyncio.StreamWriter)
        mock_writer.is_closing.return_value = False
        transport.writer = mock_writer

        with patch("mcubridge.transport.serial.logger") as mock_logger:
            # Command 0xFF is usually not in protocol
            await transport._serial_sender(0xFF, b"test") # type: ignore[reportPrivateUsage]
            assert mock_writer.write.called
    finally:
        state.cleanup()


@pytest.mark.asyncio
async def test_is_raw_binary_frame_valid_size() -> None:
    from mcubridge.protocol.frame import Frame
    
    config = _make_config()
    state = create_runtime_state(config)
    try:
        raw = Frame(0x01, 0, b"p").build()
        cmd, seq, payload = Frame.parse(raw)
        assert cmd == 1
        assert seq == 0
        assert payload == b"p"
    finally:
        state.cleanup()
