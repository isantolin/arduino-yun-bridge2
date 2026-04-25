from unittest.mock import AsyncMock, MagicMock, patch
import asyncio
import pytest
from cobs import cobs
from mcubridge.config.settings import RuntimeConfig
from mcubridge.protocol.frame import Frame
from mcubridge.protocol.protocol import Command, Status
from mcubridge.services.runtime import BridgeService
from mcubridge.state.context import create_runtime_state
from mcubridge.transport.serial import SerialTransport
from tests._helpers import make_test_config

def _make_config() -> RuntimeConfig:
    return make_test_config(
        serial_port="/dev/ttyFake",
        serial_shared_secret=b"secret1234",
        reconnect_delay=1,
    )

@pytest.mark.asyncio
async def test_serial_process_valid_frame() -> None:
    config = _make_config()
    state = create_runtime_state(config)
    try:
        mock_service = AsyncMock(spec=BridgeService)
        transport = SerialTransport(config, state, mock_service)
        
        frame = Frame(Command.CMD_CONSOLE_WRITE.value, 123, b"data")
        encoded = cobs.encode(frame.build())
        
        await transport._process_packet(encoded) # type: ignore
        
        mock_service.handle_mcu_frame.assert_awaited_once_with(
            Command.CMD_CONSOLE_WRITE.value, 123, b"data"
        )
        assert state.serial_frames_received == 1
    finally:
        state.cleanup()

@pytest.mark.asyncio
async def test_serial_process_malformed_frame() -> None:
    config = _make_config()
    state = create_runtime_state(config)
    try:
        mock_service = AsyncMock(spec=BridgeService)
        transport = SerialTransport(config, state, mock_service)
        
        # Completely invalid data for COBS
        await transport._process_packet(b"\x00\x00") # type: ignore
        
        assert state.serial_decode_errors == 1
        mock_service.handle_mcu_frame.assert_not_called()
    finally:
        state.cleanup()

@pytest.mark.asyncio
async def test_serial_send_frame() -> None:
    config = _make_config()
    state = create_runtime_state(config)
    state.serial_tx_allowed.set()
    try:
        mock_service = AsyncMock(spec=BridgeService)
        transport = SerialTransport(config, state, mock_service)
        
        mock_writer = AsyncMock(spec=asyncio.StreamWriter)
        mock_writer.is_closing.return_value = False
        transport.writer = mock_writer
        
        ok = await transport.send(Command.CMD_CONSOLE_WRITE.value, b"hi", seq=42)
        assert ok is True
        
        mock_writer.write.assert_called_once()
        assert state.serial_frames_sent == 1
    finally:
        state.cleanup()

@pytest.mark.asyncio
async def test_serial_baudrate_negotiation_flow() -> None:
    config = _make_config()
    state = create_runtime_state(config)
    state.serial_tx_allowed.set()
    try:
        mock_service = AsyncMock(spec=BridgeService)
        transport = SerialTransport(config, state, mock_service)
        transport.loop = asyncio.get_running_loop()
        
        mock_writer = AsyncMock(spec=asyncio.StreamWriter)
        mock_writer.is_closing.return_value = False
        mock_writer.transport = MagicMock()
        transport.writer = mock_writer
        
        # Start negotiation in background
        task = asyncio.create_task(transport._negotiate_baudrate(57600)) # type: ignore
        
        await asyncio.sleep(0.05)
        # Simulate MCU response
        resp = Frame(Command.CMD_SET_BAUDRATE_RESP.value, 0, b"")
        await transport._process_packet(cobs.encode(resp.build())) # type: ignore
        
        ok = await task
        assert ok is True
    finally:
        state.cleanup()
