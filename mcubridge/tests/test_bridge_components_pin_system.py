"""Pin system tests for McuBridge."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from aiomqtt.message import Message

from mcubridge.config.settings import RuntimeConfig
from mcubridge.protocol import protocol
from mcubridge.protocol.topics import Topic, topic_path
from mcubridge.services.runtime import BridgeService
from mcubridge.state.context import RuntimeState
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
    # Encode a DigitalReadResponsePacket
    payload = protocol.DigitalReadResponsePacket(pin=pin, value=value).encode()

    await service.dispatcher.dispatch_mcu_frame(
        protocol.Command.CMD_DIGITAL_READ_RESP.value, 0, payload
    )

    transport.enqueue_mqtt.assert_called_once()
    call_args = transport.enqueue_mqtt.call_args[0][0]
    assert f"digital/{pin}/state" in call_args.topic_name
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
    # Encode an AnalogReadResponsePacket
    payload = protocol.AnalogReadResponsePacket(pin=pin, value=value).encode()

    await service.dispatcher.dispatch_mcu_frame(
        protocol.Command.CMD_ANALOG_READ_RESP.value, 0, payload
    )

    transport.enqueue_mqtt.assert_called_once()
    call_args = transport.enqueue_mqtt.call_args[0][0]
    assert f"analog/{pin}/state" in call_args.topic_name
    assert call_args.payload == b"512"


@pytest.mark.asyncio
async def test_mqtt_digital_write_sends_serial_frame(
    runtime_config: RuntimeConfig,
    runtime_state: RuntimeState,
) -> None:
    transport = MqttTransport(runtime_config, runtime_state)
    service = BridgeService(runtime_config, runtime_state, transport)
    service.serial_flow.send = AsyncMock(return_value=True)

    pin = 13
    await service.handle_mqtt_message(
        Message(
            topic=topic_path(
                runtime_state.mqtt_topic_prefix, Topic.DIGITAL, str(pin), "set"
            ),
            payload=b"1",
            qos=0,
            retain=False,
            mid=1,
            properties=None,
        )
    )

    service.serial_flow.send.assert_called_once()
    args = service.serial_flow.send.call_args[0]
    assert args[0] == protocol.Command.CMD_DIGITAL_WRITE.value
    sent_payload = args[1]
    decoded = protocol.DigitalWritePacket.decode(sent_payload)
    assert decoded.pin == pin
    assert decoded.value == 1


@pytest.mark.asyncio
async def test_mqtt_analog_write_sends_serial_frame(
    runtime_config: RuntimeConfig,
    runtime_state: RuntimeState,
) -> None:
    transport = MqttTransport(runtime_config, runtime_state)
    service = BridgeService(runtime_config, runtime_state, transport)
    service.serial_flow.send = AsyncMock(return_value=True)

    pin = 11
    await service.handle_mqtt_message(
        Message(
            topic=topic_path(
                runtime_state.mqtt_topic_prefix, Topic.ANALOG, str(pin), "set"
            ),
            payload=b"128",
            qos=0,
            retain=False,
            mid=1,
            properties=None,
        )
    )

    service.serial_flow.send.assert_called_once()
    args = service.serial_flow.send.call_args[0]
    assert args[0] == protocol.Command.CMD_ANALOG_WRITE.value
    sent_payload = args[1]
    decoded = protocol.AnalogWritePacket.decode(sent_payload)
    assert decoded.pin == pin
    assert decoded.value == 128


@pytest.mark.asyncio
async def test_mqtt_pin_mode_sends_serial_frame(
    runtime_config: RuntimeConfig,
    runtime_state: RuntimeState,
) -> None:
    transport = MqttTransport(runtime_config, runtime_state)
    service = BridgeService(runtime_config, runtime_state, transport)
    service.serial_flow.send = AsyncMock(return_value=True)

    pin = 7
    await service.handle_mqtt_message(
        Message(
            topic=topic_path(
                runtime_state.mqtt_topic_prefix, Topic.DIGITAL, str(pin), "mode"
            ),
            payload=b"output",
            qos=0,
            retain=False,
            mid=1,
            properties=None,
        )
    )

    service.serial_flow.send.assert_called_once()
    args = service.serial_flow.send.call_args[0]
    assert args[0] == protocol.Command.CMD_PIN_MODE.value
    sent_payload = args[1]
    decoded = protocol.PinModePacket.decode(sent_payload)
    assert decoded.pin == pin
    assert decoded.mode == protocol.PinMode.OUTPUT


@pytest.mark.asyncio
async def test_mqtt_digital_read_sends_serial_frame(
    runtime_config: RuntimeConfig,
    runtime_state: RuntimeState,
) -> None:
    transport = MqttTransport(runtime_config, runtime_state)
    service = BridgeService(runtime_config, runtime_state, transport)
    service.serial_flow.send = AsyncMock(return_value=True)

    pin = 4
    await service.handle_mqtt_message(
        Message(
            topic=topic_path(
                runtime_state.mqtt_topic_prefix, Topic.DIGITAL, str(pin), "get"
            ),
            payload=b"",
            qos=0,
            retain=False,
            mid=1,
            properties=None,
        )
    )

    service.serial_flow.send.assert_called_once()
    args = service.serial_flow.send.call_args[0]
    assert args[0] == protocol.Command.CMD_DIGITAL_READ.value
    sent_payload = args[1]
    decoded = protocol.DigitalReadPacket.decode(sent_payload)
    assert decoded.pin == pin


@pytest.mark.asyncio
async def test_mqtt_analog_read_sends_serial_frame(
    runtime_config: RuntimeConfig,
    runtime_state: RuntimeState,
) -> None:
    transport = MqttTransport(runtime_config, runtime_state)
    service = BridgeService(runtime_config, runtime_state, transport)
    service.serial_flow.send = AsyncMock(return_value=True)

    pin = 0
    await service.handle_mqtt_message(
        Message(
            topic=topic_path(
                runtime_state.mqtt_topic_prefix, Topic.ANALOG, str(pin), "get"
            ),
            payload=b"",
            qos=0,
            retain=False,
            mid=1,
            properties=None,
        )
    )

    service.serial_flow.send.assert_called_once()
    args = service.serial_flow.send.call_args[0]
    assert args[0] == protocol.Command.CMD_ANALOG_READ.value
    sent_payload = args[1]
    decoded = protocol.AnalogReadPacket.decode(sent_payload)
    assert decoded.pin == pin


@pytest.mark.asyncio
async def test_mqtt_shell_run_invokes_process_component(
    runtime_config: RuntimeConfig,
    runtime_state: RuntimeState,
) -> None:
    transport = MqttTransport(runtime_config, runtime_state)
    transport.enqueue_mqtt = AsyncMock()
    service = BridgeService(runtime_config, runtime_state, transport)
    process = service.process

    with patch.object(process, "handle_mqtt", new_callable=AsyncMock) as mock_mqtt:
        # Re-register mock in dispatcher manually
        service.dispatcher.process = MagicMock(handle_mqtt=mock_mqtt)

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
        # ProcessComponent handles shell topics
        mock_mqtt.assert_called_once()


@pytest.mark.asyncio
async def test_mqtt_shell_kill_invokes_processonent(
    runtime_config: RuntimeConfig,
    runtime_state: RuntimeState,
) -> None:
    transport = MqttTransport(runtime_config, runtime_state)
    transport.enqueue_mqtt = AsyncMock()
    service = BridgeService(runtime_config, runtime_state, transport)
    process = service.process

    with patch.object(process, "handle_mqtt", new_callable=AsyncMock) as mock_mqtt:
        # Re-register mock in dispatcher manually
        service.dispatcher.process = MagicMock(handle_mqtt=mock_mqtt)
        pid = 21
        msg = Message(
            topic=topic_path(
                runtime_state.mqtt_topic_prefix,
                Topic.SHELL,
                "kill",
                str(pid),
            ),
            payload=b"",
            qos=0,
            retain=False,
            mid=1,
            properties=None,
        )
        # Use the official handle_mqtt_message which now uses declarative dispatch
        await service.handle_mqtt_message(msg)

        # ProcessComponent handles shell topics
        mock_mqtt.assert_called()
        route = mock_mqtt.call_args[0][0]
        assert route.segments == ("kill", str(pid))
