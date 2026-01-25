"""Additional tests for mcubridge.transport.serial_fast branch coverage."""

from __future__ import annotations

import sys
from unittest.mock import MagicMock

# Mock serial_asyncio_fast before importing the module under test
sys.modules["serial_asyncio_fast"] = MagicMock()

import asyncio
from unittest.mock import AsyncMock, MagicMock
from typing import cast

import pytest

from cobs import cobs

from mcubridge.config.settings import RuntimeConfig
from mcubridge.rpc import protocol
from mcubridge.rpc.frame import Frame
from mcubridge.rpc.protocol import Command
from mcubridge.services.runtime import BridgeService
from mcubridge.services.handshake import SerialHandshakeFatal
from mcubridge.state.context import create_runtime_state
from mcubridge.transport import serial_fast as serial


@pytest.mark.asyncio
async def test_negotiate_baudrate_success() -> None:
    config = RuntimeConfig(
        serial_port="/dev/null",
        serial_baud=protocol.DEFAULT_BAUDRATE,
        serial_safe_baud=protocol.DEFAULT_SAFE_BAUDRATE,
        mqtt_host="localhost",
        mqtt_port=1883,
        mqtt_user=None,
        mqtt_pass=None,
        mqtt_tls=False,
        mqtt_cafile=None,
        mqtt_certfile=None,
        mqtt_keyfile=None,
        mqtt_topic="br",
        allowed_commands=(),
        file_system_root="/tmp",
        process_timeout=1,
        reconnect_delay=1,
        status_interval=1,
        debug_logging=False,
        console_queue_limit_bytes=64,
        mailbox_queue_limit=2,
        mailbox_queue_bytes_limit=32,
        serial_shared_secret=b"testshared",
    )
    state = create_runtime_state(config)
    service = BridgeService(config, state)
    
    transport = serial.SerialTransport(config, state, service)
    proto = serial.BridgeSerialProtocol(service, state, asyncio.get_running_loop())
    
    # Mock write_frame to simulate sending request
    proto.write_frame = MagicMock(return_value=True) # type: ignore
    
    task = asyncio.create_task(transport._negotiate_baudrate(proto, 115200))
    await asyncio.sleep(0) # Let task start
    
    # Simulate receiving response
    assert proto._negotiation_future is not None
    proto._negotiation_future.set_result(True)
    
    ok = await task
    assert ok is True
    proto.write_frame.assert_called()


@pytest.mark.asyncio
async def test_negotiate_baudrate_timeout_returns_false(monkeypatch: pytest.MonkeyPatch) -> None:
    config = RuntimeConfig(
        serial_port="/dev/null",
        serial_baud=protocol.DEFAULT_BAUDRATE,
        serial_safe_baud=protocol.DEFAULT_SAFE_BAUDRATE,
        mqtt_host="localhost",
        mqtt_port=1883,
        mqtt_user=None,
        mqtt_pass=None,
        mqtt_tls=False,
        mqtt_cafile=None,
        mqtt_certfile=None,
        mqtt_keyfile=None,
        mqtt_topic="br",
        allowed_commands=(),
        file_system_root="/tmp",
        process_timeout=1,
        reconnect_delay=1,
        status_interval=1,
        debug_logging=False,
        console_queue_limit_bytes=64,
        mailbox_queue_limit=2,
        mailbox_queue_bytes_limit=32,
        serial_shared_secret=b"testshared",
    )
    state = create_runtime_state(config)
    service = BridgeService(config, state)
    
    transport = serial.SerialTransport(config, state, service)
    proto = serial.BridgeSerialProtocol(service, state, asyncio.get_running_loop())
    
    proto.write_frame = MagicMock(return_value=True) # type: ignore
    
    # Short timeout for test
    monkeypatch.setattr(serial, "SERIAL_BAUDRATE_NEGOTIATION_TIMEOUT", 0.01)
    
    ok = await transport._negotiate_baudrate(proto, 115200)
    assert ok is False


@pytest.mark.asyncio
async def test_packet_too_large_flushes_buffer(monkeypatch: pytest.MonkeyPatch) -> None:
    config = RuntimeConfig(
        serial_port="/dev/null",
        serial_baud=protocol.DEFAULT_BAUDRATE,
        serial_safe_baud=protocol.DEFAULT_SAFE_BAUDRATE,
        mqtt_host="localhost",
        mqtt_port=1883,
        mqtt_user=None,
        mqtt_pass=None,
        mqtt_tls=False,
        mqtt_cafile=None,
        mqtt_certfile=None,
        mqtt_keyfile=None,
        mqtt_topic="br",
        allowed_commands=(),
        file_system_root="/tmp",
        process_timeout=1,
        reconnect_delay=1,
        status_interval=1,
        debug_logging=False,
        console_queue_limit_bytes=64,
        mailbox_queue_limit=2,
        mailbox_queue_bytes_limit=32,
        serial_shared_secret=b"testshared",
    )

    state = create_runtime_state(config)
    service = BridgeService(config, state)

    proto = serial.BridgeSerialProtocol(service, state, asyncio.get_running_loop())

    # Feed oversized packet
    large_data = b"A" * (serial.MAX_SERIAL_PACKET_BYTES + 5)
    proto.data_received(large_data)

    assert state.serial_decode_errors == 1
    # Buffer should be cleared (or mostly cleared/discarding)
    # The protocol sets _discarding=True
    assert proto._discarding is True
    assert len(proto._buffer) == 0

    # Next delimiter should reset discarding
    proto.data_received(protocol.FRAME_DELIMITER)
    assert proto._discarding is False


@pytest.mark.asyncio
async def test_serial_transport_run_stops_on_handshake_fatal(monkeypatch: pytest.MonkeyPatch) -> None:
    config = RuntimeConfig(
        serial_port="/dev/null",
        serial_baud=protocol.DEFAULT_BAUDRATE,
        serial_safe_baud=protocol.DEFAULT_SAFE_BAUDRATE,
        mqtt_host="localhost",
        mqtt_port=1883,
        mqtt_user=None,
        mqtt_pass=None,
        mqtt_tls=False,
        mqtt_cafile=None,
        mqtt_certfile=None,
        mqtt_keyfile=None,
        mqtt_topic="br",
        allowed_commands=(),
        file_system_root="/tmp",
        process_timeout=1,
        reconnect_delay=1,
        status_interval=1,
        debug_logging=False,
        console_queue_limit_bytes=64,
        mailbox_queue_limit=2,
        mailbox_queue_bytes_limit=32,
        serial_shared_secret=b"testshared",
    )

    state = create_runtime_state(config)
    service = BridgeService(config, state)

    transport = serial.SerialTransport(config, state, service)

    # Mock _connect_and_run to raise Fatal
    monkeypatch.setattr(transport, "_connect_and_run", AsyncMock(side_effect=SerialHandshakeFatal("fatal")))

    sleep_spy = AsyncMock()
    monkeypatch.setattr(serial.asyncio, "sleep", sleep_spy)

    with pytest.raises(SerialHandshakeFatal):
        await transport.run()

    sleep_spy.assert_not_awaited()