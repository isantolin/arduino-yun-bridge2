"""Extended unit tests for SerialTransport implementation (SIL-2)."""

from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from mcubridge.config.settings import RuntimeConfig
from mcubridge.protocol.protocol import DEFAULT_BAUDRATE, Command
from mcubridge.protocol.frame import Frame
from mcubridge.transport.serial import SerialTransport
from mcubridge.transport.mqtt import MqttTransport
from mcubridge.services.runtime import BridgeService
from mcubridge.services.handshake import SerialHandshakeFatal
from mcubridge.state.context import create_runtime_state


def _make_config() -> RuntimeConfig:
    from tests._helpers import make_test_config
    return make_test_config(
        serial_port="/dev/null",
        serial_baud=DEFAULT_BAUDRATE,
        allowed_commands=(),
    )


@pytest.mark.asyncio
async def test_negotiate_baudrate_success() -> None:
    patch_path = "serial_asyncio_fast.open_serial_connection"
    with patch(patch_path, new_callable=AsyncMock) as mock_open:
        mock_reader = AsyncMock(spec=asyncio.StreamReader)
        mock_reader.readuntil.side_effect = asyncio.IncompleteReadError(b"", None)
        mock_writer = MagicMock(spec=asyncio.StreamWriter)
        mock_writer.transport = MagicMock()
        mock_writer.wait_closed = AsyncMock()
        mock_open.return_value = (mock_reader, mock_writer)

        config = _make_config()
        config.serial_baud = 57600  # Different from default 115200
        state = create_runtime_state(config)
        try:
            service = BridgeService(config, state, MqttTransport(config, state))
            # Mock successful negotiation in flow controller
            service.serial_flow.negotiate_baudrate = AsyncMock(return_value=True)
            # Ensure synchronize doesn't fail
            service.handshake_manager.synchronize = AsyncMock(return_value=True)
            
            transport = SerialTransport(config, state, service)
            # Switch baudrate would fail if writer.transport is not mocked properly
            with patch.object(transport, "_switch_local_baudrate"):
                await transport._retryable_run(asyncio.get_running_loop()) # type: ignore[reportPrivateUsage]
                assert transport.baudrate == 57600
        finally:
            state.cleanup()


@pytest.mark.asyncio
async def test_negotiate_baudrate_timeout() -> None:
    patch_path = "serial_asyncio_fast.open_serial_connection"
    with patch(patch_path, new_callable=AsyncMock) as mock_open:
        mock_reader = AsyncMock(spec=asyncio.StreamReader)
        mock_reader.readuntil.side_effect = asyncio.IncompleteReadError(b"", None)
        mock_writer = MagicMock(spec=asyncio.StreamWriter)
        mock_writer.transport = MagicMock()
        mock_writer.wait_closed = AsyncMock()
        mock_open.return_value = (mock_reader, mock_writer)

        config = _make_config()
        config.serial_baud = 57600
        state = create_runtime_state(config)
        try:
            service = BridgeService(config, state, MqttTransport(config, state))
            # Mock negotiation timeout
            service.serial_flow.negotiate_baudrate = AsyncMock(return_value=False)
            service.handshake_manager.synchronize = AsyncMock(return_value=True)
            
            transport = SerialTransport(config, state, service)
            await transport._retryable_run(asyncio.get_running_loop()) # type: ignore[reportPrivateUsage]
            # Should stay at default/safe baud
            assert transport.baudrate == 115200
        finally:
            state.cleanup()


@pytest.mark.timeout(5)
@pytest.mark.asyncio
async def test_retryable_run_opens_uart_at_safe_baud() -> None:
    patch_path = "serial_asyncio_fast.open_serial_connection"
    with patch(patch_path, new_callable=AsyncMock) as mock_open:
        mock_reader = AsyncMock(spec=asyncio.StreamReader)
        mock_reader.readuntil.side_effect = asyncio.IncompleteReadError(b"", None)
        mock_writer = MagicMock(spec=asyncio.StreamWriter)
        mock_writer.transport = MagicMock()
        mock_writer.wait_closed = AsyncMock()
        mock_open.return_value = (mock_reader, mock_writer)

        config = _make_config()
        state = create_runtime_state(config)
        try:
            service = BridgeService(config, state, MqttTransport(config, state))
            service.handshake_manager.synchronize = AsyncMock(return_value=True)
            transport = SerialTransport(config, state, service)
            await transport._retryable_run(asyncio.get_running_loop()) # type: ignore[reportPrivateUsage]

            mock_open.assert_called_once()
            assert mock_open.call_args.kwargs["baudrate"] == 115200
        finally:
            state.cleanup()


@pytest.mark.asyncio
async def test_serial_disconnected_hook_error() -> None:
    """Test on_serial_disconnected hook error is logged and handled."""
    patch_path = "serial_asyncio_fast.open_serial_connection"
    with patch(patch_path, new_callable=AsyncMock) as mock_open:
        mock_reader = AsyncMock(spec=asyncio.StreamReader)
        mock_reader.readuntil.side_effect = asyncio.IncompleteReadError(b"", None)
        mock_writer = MagicMock(spec=asyncio.StreamWriter)
        mock_writer.transport = MagicMock()
        mock_writer.wait_closed = AsyncMock()
        mock_open.return_value = (mock_reader, mock_writer)

        config = _make_config()
        state = create_runtime_state(config)
        try:
            service = BridgeService(config, state, MqttTransport(config, state))
            service.handshake_manager.synchronize = AsyncMock(return_value=True)
            service.on_serial_disconnected = AsyncMock(side_effect=RuntimeError("hook fail"))
            
            transport = SerialTransport(config, state, service)
            # This should not raise
            await transport._retryable_run(asyncio.get_running_loop()) # type: ignore[reportPrivateUsage]
        finally:
            state.cleanup()


@pytest.mark.asyncio
async def test_async_process_packet_os_error(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Test _process_packet handles OSError gracefully."""
    config = _make_config()
    state = create_runtime_state(config)
    try:
        service = BridgeService(config, state, MqttTransport(config, state))
        transport = SerialTransport(config, state, service)
        transport.loop = asyncio.get_running_loop()

        # Mock handle_mcu_frame to raise OSError
        service.dispatcher.dispatch_mcu_frame = AsyncMock(side_effect=OSError("Device error"))

        frame_data = Frame(
            command_id=Command.CMD_GET_VERSION.value, sequence_id=0, payload=b"\x00"
        ).build()

        from cobs import cobs
        from mcubridge.protocol.protocol import FRAME_DELIMITER
        encoded = cobs.encode(frame_data) + FRAME_DELIMITER

        caplog.set_level("ERROR")
        # Should catch and log from BridgeService.handle_mcu_frame try/except
        await transport._process_packet(encoded) # type: ignore[reportPrivateUsage]
        assert "Dispatch error" in caplog.text
    finally:
        state.cleanup()


@pytest.mark.asyncio
async def test_transport_run_handshake_fatal() -> None:
    patch_path = "serial_asyncio_fast.open_serial_connection"
    with patch(patch_path, new_callable=AsyncMock) as mock_open:
        mock_reader = AsyncMock(spec=asyncio.StreamReader)
        mock_reader.readuntil.side_effect = asyncio.IncompleteReadError(b"", None)
        mock_writer = MagicMock(spec=asyncio.StreamWriter)
        mock_writer.transport = MagicMock()
        mock_writer.wait_closed = AsyncMock()
        mock_open.return_value = (mock_reader, mock_writer)

        config = _make_config()
        state = create_runtime_state(config)
        try:
            service = BridgeService(config, state, MqttTransport(config, state))
            
            from mcubridge.services.handshake import SerialHandshakeFatal
            service.handshake_manager.synchronize = AsyncMock(side_effect=SerialHandshakeFatal("fatal"))
            
            transport = SerialTransport(config, state, service)
            # Should propagate SerialHandshakeFatal
            with pytest.raises(SerialHandshakeFatal):
                await transport._retryable_run(asyncio.get_running_loop()) # type: ignore[reportPrivateUsage]
        finally:
            state.cleanup()
