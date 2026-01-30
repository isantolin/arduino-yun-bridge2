import sys
import asyncio
from unittest.mock import MagicMock, AsyncMock

# Mock serial_asyncio_fast
mock_serial_fast = MagicMock()
_mock_proto = MagicMock()
_mock_proto._connected_future = asyncio.Future()
_mock_proto._connected_future.set_result(None)
mock_serial_fast.create_serial_connection = AsyncMock(return_value=(MagicMock(), _mock_proto))
sys.modules["serial_asyncio_fast"] = mock_serial_fast

from unittest.mock import AsyncMock  # noqa: E402
import pytest  # noqa: E402
from mcubridge.config.settings import RuntimeConfig  # noqa: E402
from mcubridge.services.runtime import BridgeService  # noqa: E402
from mcubridge.services.handshake import SerialHandshakeFatal  # noqa: E402
from mcubridge.state.context import create_runtime_state  # noqa: E402
from mcubridge.transport import serial_fast  # noqa: E402


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
async def test_negotiate_baudrate_timeout(monkeypatch: pytest.MonkeyPatch) -> None:
    config = _make_config()
    state = create_runtime_state(config)
    service = BridgeService(config, state)

    transport = serial_fast.SerialTransport(config, state, service)
    proto = serial_fast.BridgeSerialProtocol(service, state, asyncio.get_running_loop())

    proto.write_frame = MagicMock(return_value=True)  # type: ignore

    # Short timeout for test
    monkeypatch.setattr(serial_fast, "SERIAL_BAUDRATE_NEGOTIATION_TIMEOUT", 0.01)

    ok = await transport._negotiate_baudrate(proto, 115200)
    assert ok is False


@pytest.mark.asyncio
async def test_transport_run_handshake_fatal(sleep_spy: AsyncMock) -> None:
    config = _make_config()
    state = create_runtime_state(config)
    service = BridgeService(config, state)
    service.on_serial_connected = AsyncMock(side_effect=SerialHandshakeFatal("Fatal"))

    transport = serial_fast.SerialTransport(config, state, service)
    
    m_p = MagicMock()
    m_p._connected_future = asyncio.get_running_loop().create_future()
    m_p._connected_future.set_result(None)

    # Mock _connect_and_run to just call service.on_serial_connected
    with patch("mcubridge.transport.serial_fast.serial_asyncio_fast.create_serial_connection", return_value=(MagicMock(), m_p)):
        with patch.object(transport, "_connect_and_run", new_callable=AsyncMock, wraps=transport._connect_and_run):
            with pytest.raises(SerialHandshakeFatal):
                await transport.run()

    sleep_spy.assert_not_awaited()
