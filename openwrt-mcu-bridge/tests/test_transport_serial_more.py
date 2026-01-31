import sys
import asyncio
from unittest.mock import MagicMock, AsyncMock

import pytest

# Mock serial_asyncio_fast
mock_serial_fast = MagicMock()
sys.modules["serial_asyncio_fast"] = mock_serial_fast

from mcubridge.config.settings import RuntimeConfig
from mcubridge.services.runtime import BridgeService
from mcubridge.services.handshake import SerialHandshakeFatal
from mcubridge.state.context import create_runtime_state
from mcubridge.transport import serial_fast


@pytest.fixture
def sleep_spy():
    with patch("asyncio.sleep", new_callable=AsyncMock) as m:
        yield m


from unittest.mock import patch


@pytest.mark.asyncio
async def test_negotiate_baudrate_success() -> None:
    _mock_proto = MagicMock()
    _mock_proto.connected_future = asyncio.get_running_loop().create_future()
    _mock_proto.connected_future.set_result(None)

    with patch("mcubridge.transport.serial_fast.serial_asyncio_fast.create_serial_connection", new_callable=AsyncMock) as mock_create:
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


@pytest.mark.asyncio
async def test_negotiate_baudrate_timeout(sleep_spy) -> None:
    _mock_proto = MagicMock()
    _mock_proto.connected_future = asyncio.get_running_loop().create_future()
    _mock_proto.connected_future.set_result(None)

    with patch("mcubridge.transport.serial_fast.serial_asyncio_fast.create_serial_connection", new_callable=AsyncMock) as mock_create:
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
    with patch("mcubridge.transport.serial_fast.serial_asyncio_fast.create_serial_connection", new_callable=AsyncMock) as mock_create:
        mock_create.return_value = (MagicMock(), _mock_proto)

        config = _make_config()
        state = create_runtime_state(config)
        service = BridgeService(config, state)

        # Force handshake fatal error
        with patch.object(service, "on_serial_connected", side_effect=SerialHandshakeFatal("test")):
            transport = serial_fast.SerialTransport(config, state, service)
            with pytest.raises(SerialHandshakeFatal):
                await transport.run()
