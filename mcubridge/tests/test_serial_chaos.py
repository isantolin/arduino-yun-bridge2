import pytest
from unittest.mock import AsyncMock
from mcubridge.transport.serial import SerialTransport
from mcubridge.services.runtime import BridgeService
from mcubridge.state.context import create_runtime_state
from mcubridge.config.settings import RuntimeConfig


def _make_config() -> RuntimeConfig:
    return RuntimeConfig(
        serial_port="/dev/test",
        topic_prefix="br",
        serial_shared_secret=b"secret",
        file_system_root=".tmp",
    )


@pytest.mark.asyncio
async def test_abrupt_disconnect():
    config = _make_config()
    state = create_runtime_state(config)
    service = BridgeService(config, state, AsyncMock(spec=SerialTransport))
    transport = SerialTransport(config, state, service)

    mock_serial = AsyncMock()
    mock_serial.readuntil.side_effect = OSError("boom")

    await transport._read_loop(mock_serial)  # pyright: ignore[reportPrivateUsage]

    assert mock_serial.readuntil.call_count == 1
