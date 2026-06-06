import asyncio
from typing import Any
from unittest.mock import AsyncMock, patch, MagicMock

import pytest
from cobs import cobs

from mcubridge.transport.serial import SerialTransport
from mcubridge.services.runtime import BridgeService
from mcubridge.config.settings import RuntimeConfig
from mcubridge.state.context import create_runtime_state, RuntimeState
from mcubridge.protocol.frame import build_frame


@pytest.fixture
def transport_setup() -> tuple[RuntimeConfig, RuntimeState]:
    config = RuntimeConfig(mqtt_topic="br", serial_port="/dev/test")
    state = create_runtime_state(config)
    return config, state


@pytest.mark.asyncio
async def test_serial_transport_loops_final_v3(transport_setup: Any) -> None:
    config, state = transport_setup
    transport = SerialTransport(config, state, service=MagicMock(spec=BridgeService))

    mock_reader = MagicMock(spec=asyncio.StreamReader)
    mock_reader.feed_eof = __import__("unittest").mock.Mock()
    mock_writer = MagicMock(spec=asyncio.StreamWriter)
    mock_writer.write = __import__("unittest").mock.Mock()
    mock_writer.close = __import__("unittest").mock.Mock()
    transport.writer = mock_writer

    frame_bytes = build_frame(command_id=0x01, sequence_id=1, payload=b"ok")
    encoded = cobs.encode(frame_bytes) + b"\x00"

    mock_reader.read.side_effect = [
        encoded[:2],
        encoded[2:],
        b"\xff\x00",
        b"",
    ]

    try:
        await asyncio.wait_for(getattr(transport, "_read_loop")(mock_reader), 0.1)
    except TimeoutError:
        pass

    setattr(transport, "_tx_sequence_id", 0xFFFE)
    mock_writer.drain = AsyncMock()
    await transport.send_raw(0x01, b"")
    assert getattr(transport, "_tx_sequence_id") == 0xFFFF
    await transport.send_raw(0x01, b"")
    assert getattr(transport, "_tx_sequence_id") == 0


@pytest.mark.asyncio
async def test_serial_transport_negotiation_failure_final_v3(
    transport_setup: Any,
) -> None:
    config, state = transport_setup
    transport = SerialTransport(config, state, service=MagicMock(spec=BridgeService))

    setattr(transport, "_negotiation_future", asyncio.Future[Any]())

    async def _mock_wait(fut: Any, timeout: Any) -> Any:
        if fut and not fut.done():
            pass
        raise TimeoutError()

    with patch("asyncio.wait_for", _mock_wait):
        res = await getattr(transport, "_negotiate_baudrate")(9600)
        assert res is False

    negotiation_future = getattr(transport, "_negotiation_future")
    if negotiation_future and not negotiation_future.done():
        negotiation_future.set_result(False)
