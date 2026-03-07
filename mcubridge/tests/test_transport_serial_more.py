import pytest
pytestmark = pytest.mark.skip(reason="Obsolete API")
import asyncio
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from mcubridge.config.settings import RuntimeConfig
from mcubridge.services.handshake import SerialHandshakeFatal
from mcubridge.services.runtime import BridgeService
from mcubridge.state.context import create_runtime_state
from mcubridge.transport import serial as serial_fast

def _make_config() -> RuntimeConfig:
    return RuntimeConfig(
        serial_port="/dev/test0",
        serial_baud=115200,
        serial_safe_baud=115200,
        mqtt_host="localhost",
        mqtt_port=1883,
        mqtt_topic="br",
        allowed_commands=(),
        file_system_root="/tmp",
        process_timeout=5,
        reconnect_delay=1,
        serial_shared_secret=b"valid_secret_1234",
    )

@pytest.mark.asyncio
async def test_negotiate_baudrate_success() -> None:
    mock_reader = MagicMock(spec=asyncio.StreamReader)

    patch_path = "mcubridge.transport.serial.serial_asyncio_fast.open_serial_connection"
    with patch(patch_path, new_callable=AsyncMock) as mock_open:
        mock_writer = MagicMock(spec=asyncio.StreamWriter)
        mock_writer.is_closing.return_value = False
        mock_open.return_value = (mock_reader, mock_writer)

        config = _make_config()
        state = create_runtime_state(config)
        service = BridgeService(config, state)

        transport = serial_fast.SerialTransport(config, state, service)
        transport.loop = asyncio.get_running_loop()

        # Mock _serial_sender to avoid real I/O and return True
        async def mock_sender(cmd, payload):
            if transport._negotiation_future and not transport._negotiation_future.done():
                transport._negotiation_future.set_result(True)
            return True

        transport._serial_sender = mock_sender

        ok = await transport._negotiate_baudrate(mock_reader, 115200)
        assert ok is True

@pytest.mark.asyncio
async def test_negotiate_baudrate_timeout() -> None:
    mock_reader = MagicMock(spec=asyncio.StreamReader)

    patch_path = "mcubridge.transport.serial.serial_asyncio_fast.open_serial_connection"
    with patch(patch_path, new_callable=AsyncMock) as mock_open:
        mock_writer = MagicMock(spec=asyncio.StreamWriter)
        mock_writer.is_closing.return_value = False
        mock_open.return_value = (mock_reader, mock_writer)

        config = _make_config()
        state = create_runtime_state(config)
        service = BridgeService(config, state)

        transport = serial_fast.SerialTransport(config, state, service)
        transport.loop = asyncio.get_running_loop()

        # Mock sender to succeed but don't resolve future
        transport._serial_sender = AsyncMock(return_value=True)

        # Mock sleep to avoid waiting
        with patch("asyncio.sleep", AsyncMock()):
            ok = await transport._negotiate_baudrate(mock_reader, 115200)
            assert ok is False


@pytest.mark.asyncio
async def test_transport_run_handshake_fatal() -> None:
    mock_reader = MagicMock(spec=asyncio.StreamReader)
    mock_writer = MagicMock(spec=asyncio.StreamWriter)
    mock_writer.transport = MagicMock()

    patch_path = "mcubridge.transport.serial.serial_asyncio_fast.open_serial_connection"
    with patch(patch_path, new_callable=AsyncMock) as mock_open:
        mock_open.return_value = (mock_reader, mock_writer)

        config = _make_config()
        state = create_runtime_state(config)
        service = BridgeService(config, state)

        # Force handshake fatal error
        with (
            patch.object(service, "on_serial_connected", side_effect=SerialHandshakeFatal("test")),
            patch.object(serial_fast.SerialTransport, "_toggle_dtr", new_callable=AsyncMock),
        ):
            transport = serial_fast.SerialTransport(config, state, service)
            with pytest.raises(SerialHandshakeFatal):
                await transport.run()


@pytest.mark.asyncio
async def test_serial_disconnected_hook_error(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Test on_serial_disconnected hook error is logged and handled."""
    mock_reader = AsyncMock(spec=asyncio.StreamReader)
    # Return EOF immediately to terminate loop
    mock_reader.readuntil.side_effect = asyncio.IncompleteReadError(b"", None)
    mock_writer = MagicMock(spec=asyncio.StreamWriter)
    mock_writer.transport = MagicMock()
    mock_writer.wait_closed = AsyncMock()

    patch_path = "mcubridge.transport.serial.serial_asyncio_fast.open_serial_connection"
    with patch(patch_path, new_callable=AsyncMock) as mock_open:
        mock_open.return_value = (mock_reader, mock_writer)

        config = _make_config()
        state = create_runtime_state(config)
        service = BridgeService(config, state)

        # Make on_serial_disconnected raise
        async def _raise_error() -> None:
            raise RuntimeError("disconnected hook error")

        transport = serial_fast.SerialTransport(config, state, service)
        orig_run = serial_fast.SerialTransport._retryable_run.__wrapped__

        with (
            patch.object(transport, "_toggle_dtr", new_callable=AsyncMock),
            patch.object(service, "on_serial_connected", new_callable=AsyncMock),
            patch.object(service, "on_serial_disconnected", side_effect=_raise_error),
        ):
            caplog.set_level("ERROR")

            try:
                # Use a timeout to ensure the test doesn't block forever
                await asyncio.wait_for(
                    orig_run(transport, asyncio.get_running_loop()),
                    timeout=5.0
                )
            except (ConnectionError, asyncio.TimeoutError, RuntimeError):

                pass

            assert any("error" in r.getMessage().lower() for r in caplog.records)


@pytest.mark.asyncio
async def test_async_process_packet_os_error(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Test _async_process_packet handles OSError gracefully."""
    config = _make_config()
    state = create_runtime_state(config)
    service = BridgeService(config, state)

    transport = serial_fast.SerialTransport(config, state, service)
    transport.loop = asyncio.get_running_loop()

    # Mock handle_mcu_frame to raise OSError
    async def _raise_os_error(cmd: int, payload: bytes) -> None:
        raise OSError("Device error")

    service.handle_mcu_frame = _raise_os_error  # type: ignore

    from cobs.cobs import encode as cobs_encode
    from mcubridge.protocol.frame import Frame
    from mcubridge.protocol.protocol import Command

    frame = Frame(command_id=Command.CMD_GET_VERSION.value, payload=b"\x00").to_bytes()
    encoded = cobs_encode(frame)

    caplog.set_level("ERROR")
    await transport._async_process_packet(encoded)

    assert any("error" in r.getMessage().lower() for r in caplog.records)
    assert any("dispatch" in r.getMessage().lower() for r in caplog.records)
