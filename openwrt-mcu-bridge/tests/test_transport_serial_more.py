import asyncio
import sys
from unittest.mock import AsyncMock, MagicMock

import pytest

# Mock serial_asyncio_fast
mock_serial_fast = MagicMock()
sys.modules["serial_asyncio_fast"] = mock_serial_fast

from mcubridge.config.settings import RuntimeConfig  # noqa: E402
from mcubridge.services.handshake import SerialHandshakeFatal  # noqa: E402
from mcubridge.services.runtime import BridgeService  # noqa: E402
from mcubridge.state.context import create_runtime_state  # noqa: E402
from mcubridge.transport import serial as serial_fast  # noqa: E402


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


from unittest.mock import patch  # noqa: E402


@pytest.mark.asyncio
async def test_negotiate_baudrate_success() -> None:
    _mock_proto = MagicMock()
    _mock_proto.connected_future = asyncio.get_running_loop().create_future()
    _mock_proto.connected_future.set_result(None)

    patch_path = "mcubridge.transport.serial.serial_asyncio_fast.create_serial_connection"
    with patch(patch_path, new_callable=AsyncMock) as mock_create:
        mock_create.return_value = (MagicMock(), _mock_proto)

        config = _make_config()
        state = create_runtime_state(config)
        service = BridgeService(config, state)

        transport = serial_fast.SerialTransport(config, state, service)
        proto = serial_fast.BridgeSerialProtocol(service, state, asyncio.get_running_loop())

        # Mock write_frame to simulate sending request
        proto.write_frame = MagicMock(return_value=True)  # type: ignore

        task = asyncio.create_task(transport._negotiate_baudrate(proto, 115200))
        await asyncio.sleep(0)  # Let task start

        # Simulate receiving response
        assert proto.negotiation_future is not None
        proto.negotiation_future.set_result(True)

        ok = await task
        assert ok is True


@pytest.mark.asyncio
async def test_negotiate_baudrate_timeout(sleep_spy) -> None:
    _mock_proto = MagicMock()
    _mock_proto.connected_future = asyncio.get_running_loop().create_future()
    _mock_proto.connected_future.set_result(None)

    patch_path = "mcubridge.transport.serial.serial_asyncio_fast.create_serial_connection"
    with patch(patch_path, new_callable=AsyncMock) as mock_create:
        mock_create.return_value = (MagicMock(), _mock_proto)

        config = _make_config()
        state = create_runtime_state(config)
        service = BridgeService(config, state)

        transport = serial_fast.SerialTransport(config, state, service)
        proto = serial_fast.BridgeSerialProtocol(service, state, asyncio.get_running_loop())

        # Mock write_frame
        proto.write_frame = MagicMock(return_value=True)  # type: ignore

        # Run negotiation, which should timeout after 3 attempts
        ok = await transport._negotiate_baudrate(proto, 115200)
        assert ok is False
        assert sleep_spy.call_count >= 2


@pytest.mark.asyncio
async def test_transport_run_handshake_fatal() -> None:
    _mock_proto = MagicMock()
    _mock_proto.connected_future = asyncio.get_running_loop().create_future()
    _mock_proto.connected_future.set_result(None)

    # Patch create_serial_connection where it is used in serial_fast
    patch_path = "mcubridge.transport.serial.serial_asyncio_fast.create_serial_connection"
    with patch(patch_path, new_callable=AsyncMock) as mock_create:
        mock_create.return_value = (MagicMock(), _mock_proto)

        config = _make_config()
        state = create_runtime_state(config)
        service = BridgeService(config, state)

        # Force handshake fatal error
        with patch.object(service, "on_serial_connected", side_effect=SerialHandshakeFatal("test")):
            transport = serial_fast.SerialTransport(config, state, service)
            with pytest.raises(SerialHandshakeFatal):
                await transport.run()


@pytest.mark.asyncio
async def test_negotiate_baudrate_write_fails() -> None:
    """Test baudrate negotiation handles write_frame failure."""
    config = _make_config()
    state = create_runtime_state(config)
    service = BridgeService(config, state)

    transport = serial_fast.SerialTransport(config, state, service)
    proto = serial_fast.BridgeSerialProtocol(service, state, asyncio.get_running_loop())

    # Mock write_frame to return False (write failure)
    proto.write_frame = MagicMock(return_value=False)  # type: ignore

    ok = await transport._negotiate_baudrate(proto, 115200)
    assert ok is False


@pytest.mark.asyncio
async def test_serial_disconnected_hook_error(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Test on_serial_disconnected hook error is logged and handled."""
    _mock_proto = MagicMock()
    _mock_proto.connected_future = asyncio.get_running_loop().create_future()
    _mock_proto.connected_future.set_result(None)
    _mock_transport = MagicMock()
    _mock_transport.is_closing.return_value = True  # Close immediately

    patch_path = "mcubridge.transport.serial.serial_asyncio_fast.create_serial_connection"
    with patch(patch_path, new_callable=AsyncMock) as mock_create:
        mock_create.return_value = (_mock_transport, _mock_proto)

        config = _make_config()
        state = create_runtime_state(config)
        service = BridgeService(config, state)

        # Make on_serial_disconnected raise
        async def _raise_error() -> None:
            raise RuntimeError("disconnected hook error")

        with patch.object(service, "on_serial_connected", new_callable=AsyncMock), \
             patch.object(service, "on_serial_disconnected", side_effect=_raise_error):
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

    loop = asyncio.get_running_loop()
    proto = serial_fast.BridgeSerialProtocol(service, state, loop)

    # Mock handle_mcu_frame to raise OSError
    async def _raise_os_error(cmd: int, payload: bytes) -> None:
        raise OSError("Device error")

    service.handle_mcu_frame = _raise_os_error  # type: ignore

    from cobs import cobs
    from mcubridge.protocol.frame import Frame
    from mcubridge.protocol.protocol import Command

    # Use a valid command ID (>= STATUS_CODE_MIN) so the frame passes
    # semantic validation and reaches handle_mcu_frame where the OSError
    # is raised.
    frame = Frame(command_id=Command.CMD_GET_VERSION.value, payload=b"\x00").to_bytes()
    encoded = cobs.encode(frame)

    caplog.set_level("ERROR")
    await proto._async_process_packet(encoded)

    assert state.serial_decode_errors > 0
    assert any("OS error" in r.getMessage() for r in caplog.records)


@pytest.mark.asyncio
async def test_async_process_packet_crc_mismatch(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Test _async_process_packet records CRC error on crc mismatch."""
    config = _make_config()
    state = create_runtime_state(config)
    service = BridgeService(config, state)

    loop = asyncio.get_running_loop()
    proto = serial_fast.BridgeSerialProtocol(service, state, loop)

    from cobs import cobs
    from mcubridge.protocol.frame import Frame

    # Create valid frame bytes then corrupt CRC
    frame = Frame(command_id=0x01, payload=b"\x00").to_bytes()
    corrupted = bytearray(frame)
    corrupted[-1] ^= 0xFF  # Flip CRC byte
    encoded = cobs.encode(bytes(corrupted))

    caplog.set_level("DEBUG")
    await proto._async_process_packet(encoded)

    # Should record decode error (CRC validation happens in Frame.from_bytes)
    assert state.serial_decode_errors > 0


@pytest.mark.asyncio
async def test_process_packet_invalid_type() -> None:
    """Test _async_process_packet handles non-binary packet type."""
    config = _make_config()
    state = create_runtime_state(config)
    service = BridgeService(config, state)

    loop = asyncio.get_running_loop()
    proto = serial_fast.BridgeSerialProtocol(service, state, loop)

    # Empty packet should be rejected by _is_binary_packet
    await proto._async_process_packet(b"")

    assert state.serial_decode_errors > 0


@pytest.mark.asyncio
async def test_data_received_discard_mode() -> None:
    """Test data_received handles discard mode for oversized packets."""
    config = _make_config()
    state = create_runtime_state(config)
    service = BridgeService(config, state)

    loop = asyncio.get_running_loop()
    proto = serial_fast.BridgeSerialProtocol(service, state, loop)

    # Trigger overflow to enter discard mode
    oversized = bytes([0xAA] * (serial_fast.MAX_SERIAL_PACKET_BYTES + 10))
    proto.data_received(oversized)

    # Verify discard mode was activated
    assert proto._discarding is True
    assert state.serial_decode_errors > 0

    # Now receive a delimiter to exit discard mode
    proto.data_received(b"\x00")
    assert proto._discarding is False


@pytest.mark.asyncio
async def test_write_frame_transport_closed() -> None:
    """Test write_frame returns False when transport is closed."""
    config = _make_config()
    state = create_runtime_state(config)
    service = BridgeService(config, state)

    loop = asyncio.get_running_loop()
    proto = serial_fast.BridgeSerialProtocol(service, state, loop)

    # No transport set
    proto.transport = None
    result = proto.write_frame(0x01, b"test")
    assert result is False

    # Transport closing
    mock_transport = MagicMock()
    mock_transport.is_closing.return_value = True
    proto.transport = mock_transport
    result = proto.write_frame(0x01, b"test")
    assert result is False


@pytest.mark.asyncio
async def test_connection_lost_sets_future_exception() -> None:
    """Test connection_lost sets connected_future exception if not done."""
    config = _make_config()
    state = create_runtime_state(config)
    service = BridgeService(config, state)

    loop = asyncio.get_running_loop()
    proto = serial_fast.BridgeSerialProtocol(service, state, loop)

    # Reset connected_future to not done
    proto.connected_future = loop.create_future()

    exc = IOError("Connection dropped")
    proto.connection_lost(exc)

    assert proto.connected_future.done()
    with pytest.raises(IOError):
        proto.connected_future.result()


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
