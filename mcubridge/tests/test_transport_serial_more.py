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


@pytest.fixture
def sleep_spy():
    with patch("asyncio.sleep", new_callable=AsyncMock) as m:
        yield m


@pytest.mark.asyncio
async def test_negotiate_baudrate_success() -> None:
    mock_reader = MagicMock(spec=asyncio.StreamReader)

    # Mock open_serial_connection
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

        # Simulate receiving response via the future
        async def simulate_resp():
            await asyncio.sleep(0.1)
            transport._negotiation_future.set_result(True)

        asyncio.create_task(simulate_resp())

        # reader.readuntil will be called by temp_reader inside negotiate
        mock_reader.readuntil.side_effect = asyncio.CancelledError # Exit loop

        ok = await transport._negotiate_baudrate(mock_reader, 115200)
        assert ok is True


@pytest.mark.asyncio
async def test_negotiate_baudrate_timeout(sleep_spy) -> None:
    mock_reader = MagicMock(spec=asyncio.StreamReader)
    # mock_reader.readuntil never returns to trigger timeout
    mock_reader.readuntil.return_value = asyncio.get_running_loop().create_future()

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

        # Run negotiation, which should timeout after 3 attempts
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
        with patch.object(service, "on_serial_connected", side_effect=SerialHandshakeFatal("test")):
            transport = serial_fast.SerialTransport(config, state, service)
            with pytest.raises(SerialHandshakeFatal):
                await transport.run()


@pytest.mark.asyncio
async def test_serial_disconnected_hook_error(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Test on_serial_disconnected hook error is logged and handled."""
    mock_reader = MagicMock(spec=asyncio.StreamReader)
    mock_writer = MagicMock(spec=asyncio.StreamWriter)
    mock_writer.transport = MagicMock()
    mock_writer.is_closing.return_value = True # Stop immediately

    patch_path = "mcubridge.transport.serial.serial_asyncio_fast.open_serial_connection"
    with patch(patch_path, new_callable=AsyncMock) as mock_open:
        mock_open.return_value = (mock_reader, mock_writer)

        config = _make_config()
        state = create_runtime_state(config)
        service = BridgeService(config, state)

        # Make on_serial_disconnected raise
        async def _raise_error() -> None:
            raise RuntimeError("disconnected hook error")

        with (
            patch.object(service, "on_serial_connected", new_callable=AsyncMock),
            patch.object(service, "on_serial_disconnected", side_effect=_raise_error),
        ):
            transport = serial_fast.SerialTransport(config, state, service)
            caplog.set_level("WARNING")

            try:
                await transport._connect_and_run(asyncio.get_running_loop())
            except ConnectionError:
                pass

            assert any("disconnected" in r.getMessage().lower() for r in caplog.records)


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

    from cobs import cobs
    from mcubridge.protocol.frame import Frame
    from mcubridge.protocol.protocol import Command

    frame = Frame(command_id=Command.CMD_GET_VERSION.value, payload=b"\x00").to_bytes()
    encoded = cobs.encode(frame)

    caplog.set_level("ERROR")
    await transport._async_process_packet(encoded)

    assert state.serial_decode_errors > 0
    assert any("OS error" in r.getMessage() for r in caplog.records)


def test_serial_sender_not_ready_logs_warning(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Test serial_sender_not_ready logs warning and returns False."""
    import asyncio

    caplog.set_level("WARNING")

    async def _run() -> bool:
        return await serial_fast.serial_sender_not_ready(0x01, b"test")

    result = asyncio.run(_run())
    assert result is False
    assert any("disconnected" in r.getMessage().lower() for r in caplog.records)
