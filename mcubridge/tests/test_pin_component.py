"""Unit tests for mcubridge.services.pin (SIL-2)."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest
from mcubridge.config.settings import RuntimeConfig
from mcubridge.protocol import structures
from mcubridge.protocol.protocol import Command
from mcubridge.protocol.topics import Topic
from mcubridge.services.pin import PinComponent
from mcubridge.state.context import RuntimeState, create_runtime_state
from tests._helpers import make_mqtt_msg, make_route


@pytest.fixture
def runtime_config() -> RuntimeConfig:
    import tempfile

    return RuntimeConfig(
        serial_port="/dev/null",
        mqtt_topic="br",
        file_system_root=tempfile.mkdtemp(prefix="mcubridge-test-fs-"),
        mqtt_spool_dir=tempfile.mkdtemp(prefix="mcubridge-test-spool-"),
        serial_shared_secret=b"s_e_c_r_e_t_mock",
    )


@pytest.fixture
def runtime_state(runtime_config: RuntimeConfig) -> RuntimeState:
    state = create_runtime_state(runtime_config)
    return state


@pytest.fixture
def serial_flow() -> MagicMock:
    sf = MagicMock()
    sf.send = AsyncMock(return_value=True)
    sf.acknowledge = AsyncMock()
    return sf


@pytest.fixture
def mqtt_flow() -> MagicMock:
    mf = MagicMock()
    mf.publish = AsyncMock()
    mf.enqueue_mqtt = AsyncMock()
    return mf


@pytest.mark.asyncio
async def test_mqtt_digital_write_sends_frame(
    serial_flow: MagicMock,
    mqtt_flow: MagicMock,
    runtime_config: RuntimeConfig,
    runtime_state: RuntimeState,
) -> None:
    component = PinComponent(runtime_config, runtime_state, serial_flow, mqtt_flow)

    await component.handle_mqtt(
        make_route(Topic.DIGITAL, "13"),
        make_mqtt_msg("1"),
    )

    serial_flow.send.assert_called_once_with(
        Command.CMD_DIGITAL_WRITE.value,
        structures.DigitalWritePacket(pin=13, value=1).encode(),
    )


@pytest.mark.asyncio
async def test_mqtt_analog_read_tracks_pending_queue(
    serial_flow: MagicMock,
    mqtt_flow: MagicMock,
    runtime_config: RuntimeConfig,
    runtime_state: RuntimeState,
) -> None:
    component = PinComponent(runtime_config, runtime_state, serial_flow, mqtt_flow)

    await component.handle_mqtt(
        make_route(Topic.ANALOG, "A1", "read"),
        make_mqtt_msg(""),
    )

    assert len(runtime_state.pending_analog_reads) == 1
    assert runtime_state.pending_analog_reads[0].pin == 1
    serial_flow.send.assert_called_once_with(
        Command.CMD_ANALOG_READ.value,
        structures.PinReadPacket(pin=1).encode(),
    )


@pytest.mark.asyncio
async def test_mcu_analog_read_response_publishes_to_mqtt(
    serial_flow: MagicMock,
    mqtt_flow: MagicMock,
    runtime_config: RuntimeConfig,
    runtime_state: RuntimeState,
) -> None:
    component = PinComponent(runtime_config, runtime_state, serial_flow, mqtt_flow)

    # 1. MCU sends response for A0
    payload = structures.AnalogReadResponsePacket(value=512).encode()

    await component.handle_analog_read_resp(0, payload)

    # Verify MQTT publish
    mqtt_flow.publish.assert_called_once()
    args, kwargs = mqtt_flow.publish.call_args
    # Topic check
    topic = kwargs.get("topic") or args[0]
    assert "a/value" in topic
    # Value check
    pld = kwargs.get("payload") or args[1]
    assert pld == b"512"


@pytest.mark.asyncio
async def test_mqtt_analog_write_sends_frame(
    serial_flow: MagicMock,
    mqtt_flow: MagicMock,
    runtime_config: RuntimeConfig,
    runtime_state: RuntimeState,
) -> None:
    component = PinComponent(runtime_config, runtime_state, serial_flow, mqtt_flow)

    await component.handle_mqtt(
        make_route(Topic.ANALOG, "A1"),
        make_mqtt_msg("10"),
    )

    serial_flow.send.assert_called_once_with(
        Command.CMD_ANALOG_WRITE.value,
        structures.AnalogWritePacket(pin=1, value=10).encode(),
    )
