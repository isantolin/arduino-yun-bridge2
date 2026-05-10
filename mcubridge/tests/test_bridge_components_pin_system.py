"""Pin system tests for McuBridge."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest
import msgspec
from aiomqtt.message import Message

from mcubridge.config.settings import RuntimeConfig
from mcubridge.protocol import protocol, structures
from mcubridge.protocol.topics import Topic, topic_path
from mcubridge.services.runtime import BridgeService
from mcubridge.state.context import RuntimeState, PendingPinRequest
from mcubridge.transport import MqttTransport


@pytest.mark.asyncio
async def test_mcu_digital_read_response_publishes_to_mqtt(
    runtime_config: RuntimeConfig,
    runtime_state: RuntimeState,
) -> None:
    transport = MqttTransport(runtime_config, runtime_state)
    transport.enqueue_mqtt = AsyncMock()
    service = BridgeService(runtime_config, runtime_state, transport)

    pin = 13
    value = 1
    # [SIL-2] Populate pending queue so component knows the pin
    runtime_state.pending_digital_reads.append(PendingPinRequest(pin, None))

    payload = msgspec.msgpack.encode(structures.DigitalReadResponsePacket(value))

    await service.dispatch_mcu_frame(
        protocol.Command.CMD_DIGITAL_READ_RESP.value, 0, payload
    )

    transport.enqueue_mqtt.assert_called_once()
    call_args = transport.enqueue_mqtt.call_args[0][0]
    assert f"d/{pin}/value" in call_args.topic_name
    assert call_args.payload == b"1"


@pytest.mark.asyncio
async def test_mcu_analog_read_response_publishes_to_mqtt(
    runtime_config: RuntimeConfig,
    runtime_state: RuntimeState,
) -> None:
    transport = MqttTransport(runtime_config, runtime_state)
    transport.enqueue_mqtt = AsyncMock()
    service = BridgeService(runtime_config, runtime_state, transport)

    pin = 3
    value = 512
    # [SIL-2] Populate pending queue so component knows the pin
    runtime_state.pending_analog_reads.append(PendingPinRequest(pin, None))

    payload = msgspec.msgpack.encode(structures.AnalogReadResponsePacket(value))

    await service.dispatch_mcu_frame(
        protocol.Command.CMD_ANALOG_READ_RESP.value, 0, payload
    )

    transport.enqueue_mqtt.assert_called_once()
    call_args = transport.enqueue_mqtt.call_args[0][0]
    assert f"a/{pin}/value" in call_args.topic_name
    assert call_args.payload == b"512"


@pytest.mark.asyncio
async def test_mqtt_digital_write_sends_serial_frame(
    runtime_config: RuntimeConfig,
    runtime_state: RuntimeState,
) -> None:
    transport = MqttTransport(runtime_config, runtime_state)
    service = BridgeService(runtime_config, runtime_state, transport)
    service.serial_flow.send = AsyncMock(return_value=True)
    service.is_topic_action_allowed = lambda _t, _a: True  # type: ignore

    pin = 13
    # Digital write uses 1 segment: prefix/d/PIN
    msg = Message(
        topic=topic_path(runtime_state.mqtt_topic_prefix, Topic.DIGITAL, str(pin)),
        payload=b"1",
        qos=0,
        retain=False,
        mid=1,
        properties=None,
    )
    await service.handle_mqtt_message(msg)
    assert service.serial_flow.send.called


@pytest.mark.asyncio
async def test_mqtt_analog_write_sends_serial_frame(
    runtime_config: RuntimeConfig,
    runtime_state: RuntimeState,
) -> None:
    transport = MqttTransport(runtime_config, runtime_state)
    service = BridgeService(runtime_config, runtime_state, transport)
    service.serial_flow.send = AsyncMock(return_value=True)
    service.is_topic_action_allowed = lambda _t, _a: True  # type: ignore

    pin = 11
    # Analog write uses 1 segment: prefix/a/PIN
    msg = Message(
        topic=topic_path(runtime_state.mqtt_topic_prefix, Topic.ANALOG, str(pin)),
        payload=b"128",
        qos=0,
        retain=False,
        mid=1,
        properties=None,
    )
    await service.handle_mqtt_message(msg)
    assert service.serial_flow.send.called


@pytest.mark.asyncio
async def test_mqtt_pin_mode_sends_serial_frame(
    runtime_config: RuntimeConfig,
    runtime_state: RuntimeState,
) -> None:
    transport = MqttTransport(runtime_config, runtime_state)
    service = BridgeService(runtime_config, runtime_state, transport)
    service.serial_flow.send = AsyncMock(return_value=True)
    service.is_topic_action_allowed = lambda _t, _a: True  # type: ignore

    pin = 7
    # Mode uses 2 segments: prefix/d/PIN/mode
    msg = Message(
        topic=topic_path(
            runtime_state.mqtt_topic_prefix, Topic.DIGITAL, str(pin), "mode"
        ),
        payload=b"1",  # OUTPUT
        qos=0,
        retain=False,
        mid=1,
        properties=None,
    )
    await service.handle_mqtt_message(msg)
    assert service.serial_flow.send.called


@pytest.mark.asyncio
async def test_mqtt_digital_read_sends_serial_frame(
    runtime_config: RuntimeConfig,
    runtime_state: RuntimeState,
) -> None:
    transport = MqttTransport(runtime_config, runtime_state)
    service = BridgeService(runtime_config, runtime_state, transport)
    service.serial_flow.send = AsyncMock(return_value=True)
    service.is_topic_action_allowed = lambda _t, _a: True  # type: ignore

    pin = 4
    # Read uses 2 segments: prefix/d/PIN/read
    msg = Message(
        topic=topic_path(
            runtime_state.mqtt_topic_prefix, Topic.DIGITAL, str(pin), "read"
        ),
        payload=b"",
        qos=0,
        retain=False,
        mid=1,
        properties=None,
    )
    await service.handle_mqtt_message(msg)
    assert service.serial_flow.send.called


@pytest.mark.asyncio
async def test_mqtt_analog_read_sends_serial_frame(
    runtime_config: RuntimeConfig,
    runtime_state: RuntimeState,
) -> None:
    transport = MqttTransport(runtime_config, runtime_state)
    service = BridgeService(runtime_config, runtime_state, transport)
    service.serial_flow.send = AsyncMock(return_value=True)
    service.is_topic_action_allowed = lambda _t, _a: True  # type: ignore

    pin = 0
    # Read uses 2 segments: prefix/a/PIN/read
    msg = Message(
        topic=topic_path(
            runtime_state.mqtt_topic_prefix, Topic.ANALOG, str(pin), "read"
        ),
        payload=b"",
        qos=0,
        retain=False,
        mid=1,
        properties=None,
    )
    await service.handle_mqtt_message(msg)
    assert service.serial_flow.send.called


@pytest.mark.asyncio
async def test_mqtt_shell_run_invokes_process_component(
    runtime_config: RuntimeConfig,
    runtime_state: RuntimeState,
) -> None:
    transport = MqttTransport(runtime_config, runtime_state)
    transport.enqueue_mqtt = AsyncMock()
    service = BridgeService(runtime_config, runtime_state, transport)
    service.is_topic_action_allowed = lambda _t, _a: True  # type: ignore
    process = service.process

    with patch.object(process, "handle_mqtt", new_callable=AsyncMock) as mock_mqtt:
        # Re-register mock in dispatcher manually
        service.process = MagicMock(handle_mqtt=mock_mqtt)

        await service.handle_mqtt_message(
            Message(
                topic=topic_path(runtime_state.mqtt_topic_prefix, Topic.SHELL, "run"),
                payload=b'{"command": "ls"}',
                qos=0,
                retain=False,
                mid=1,
                properties=None,
            )
        )
        assert mock_mqtt.called
