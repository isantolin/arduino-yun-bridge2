"""Extra edge-case tests for ConsoleComponent (SIL-2)."""

from __future__ import annotations
from mcubridge.services.serial_flow import SerialFlowController
from mcubridge.transport.mqtt import MqttTransport
import msgspec

import os
import time
from unittest.mock import AsyncMock

import pytest
from mcubridge.config.settings import RuntimeConfig
from mcubridge.protocol.structures import ConsoleWritePacket
from mcubridge.services.console import ConsoleComponent
from mcubridge.state.context import create_runtime_state


@pytest.mark.asyncio
async def test_console_handle_write_edge_cases() -> None:
    config = RuntimeConfig(
        serial_shared_secret=b"secret_1234",
        mqtt_spool_dir=os.path.abspath(
            f".tmp_tests/mcubridge-test-console-{os.getpid()}-{time.time_ns()}"
        ),
    )
    state = create_runtime_state(config)
    try:
        serial_flow = AsyncMock(spec=SerialFlowController)
        serial_flow.send = AsyncMock(return_value=True)
        mqtt_flow = AsyncMock(spec=MqttTransport)
        mqtt_flow.enqueue_mqtt = AsyncMock()

        comp = ConsoleComponent(
            config=config, state=state, serial_flow=serial_flow, mqtt_flow=mqtt_flow
        )

        # 1. Malformed payload
        await comp.handle_write(0, b"\xff\xff")
        assert not mqtt_flow.enqueue_mqtt.called

        # 2. Empty data in packet
        empty_pkt = msgspec.msgpack.encode(ConsoleWritePacket(data=b""))
        await comp.handle_write(1, empty_pkt)
        assert not mqtt_flow.enqueue_mqtt.called

        # 3. Successful write
        valid_pkt = msgspec.msgpack.encode(ConsoleWritePacket(data=b"hello"))
        await comp.handle_write(2, valid_pkt)
        assert mqtt_flow.enqueue_mqtt.called
    finally:
        state.cleanup()


@pytest.mark.asyncio
async def test_console_mqtt_input_error_paths() -> None:
    config = RuntimeConfig(serial_shared_secret=b"secret_1234")
    state = create_runtime_state(config)
    try:
        serial_flow = AsyncMock(spec=SerialFlowController)
        # Simulate serial failure
        serial_flow.send = AsyncMock(return_value=False)
        mqtt_flow = AsyncMock(spec=MqttTransport)

        comp = ConsoleComponent(
            config=config, state=state, serial_flow=serial_flow, mqtt_flow=mqtt_flow
        )

        # Sending input when serial fails should queue it
        await comp._handle_mqtt_input(b"lost-data")  # type: ignore[reportPrivateUsage]
        assert len(state.console_to_mcu_queue) == 1
    finally:
        state.cleanup()
