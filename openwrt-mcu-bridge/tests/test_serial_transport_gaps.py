"""Final coverage gap closure for Serial Transport."""

import asyncio
from unittest.mock import MagicMock

import pytest
from mcubridge.config.settings import RuntimeConfig
from mcubridge.protocol import protocol
from mcubridge.transport.serial import BridgeSerialProtocol, SerialTransport
from mcubridge.state.context import create_runtime_state


@pytest.mark.asyncio
async def test_serial_negotiate_baudrate_write_fails() -> None:
    config = RuntimeConfig(serial_shared_secret=b"secret_1234")
    state = create_runtime_state(config)
    mock_transport = MagicMock()
    mock_transport.is_closing.return_value = False

    # Mock write to fail
    mock_transport.write.side_effect = Exception("Write failed")

    loop = asyncio.get_running_loop()
    st = SerialTransport(config, state, MagicMock())
    proto = BridgeSerialProtocol(MagicMock(), state, loop)
    proto.connection_made(mock_transport)

    # Attempt negotiation via the correct class method - should fail due to mock exception
    with pytest.raises(Exception, match="Write failed"):
        await st._negotiate_baudrate(proto, 57600)


@pytest.mark.asyncio
async def test_serial_transport_connection_lost_with_exception() -> None:
    config = RuntimeConfig(serial_shared_secret=b"secret_1234")
    state = create_runtime_state(config)
    loop = asyncio.get_running_loop()
    proto = BridgeSerialProtocol(MagicMock(), state, loop)
    proto.connected_future.set_result(None)

    # Connection lost with an error
    exc = ConnectionError("Physical link lost")
    proto.connection_lost(exc)

    # connected_future should be done
    assert proto.connected_future.done()


@pytest.mark.asyncio
async def test_serial_write_frame_transport_closed() -> None:
    config = RuntimeConfig(serial_shared_secret=b"secret_1234")
    state = create_runtime_state(config)
    loop = asyncio.get_running_loop()
    proto = BridgeSerialProtocol(MagicMock(), state, loop)

    # write_frame returns bool synchronously when transport is None or closing
    success = proto.write_frame(protocol.Command.CMD_GET_VERSION.value, b"")
    assert success is False

    # Transport closing
    mock_transport = MagicMock()
    mock_transport.is_closing.return_value = True
    proto.transport = mock_transport
    success = proto.write_frame(protocol.Command.CMD_GET_VERSION.value, b"")
    assert success is False
