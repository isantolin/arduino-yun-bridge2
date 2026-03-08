"""Unit tests for Bridge components (Pin, System, Shell)."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest
from mcubridge.protocol import protocol, structures
from mcubridge.protocol.protocol import Command, Topic
from mcubridge.services.pin import PinComponent
from mcubridge.services.system import SystemComponent
from mcubridge.services.shell import ShellComponent
from mcubridge.state.context import PendingPinRequest


@pytest.fixture
def mock_context():
    """Provides a mocked BridgeContext for component testing."""
    ctx = MagicMock()
    ctx.publish = AsyncMock()
    ctx.send_frame = AsyncMock(return_value=True)
    ctx.acknowledge_frame = AsyncMock()
    return ctx


@pytest.mark.asyncio
async def test_pin_digital_read_resp_publishes_to_mqtt(runtime_config, runtime_state, mock_context) -> None:
    comp = PinComponent(runtime_config, runtime_state, mock_context)  # type: ignore

    # Setup pending request
    runtime_state.pending_digital_reads.append(PendingPinRequest(pin=7, reply_context=None))

    # [SIL-2] Payload: pin(7) + value(1)
    payload = structures.DigitalReadResponsePacket.SCHEMA.build({"pin": 7, "value": 1})

    await comp.handle_digital_read_resp(payload)

    # Verify MQTT publish
    mock_context.publish.assert_called_once()
    args = mock_context.publish.call_args[1]
    assert args["payload"] == b"1"
    assert "d/7/value" in args["topic"]


@pytest.mark.asyncio
async def test_pin_analog_read_resp_publishes_to_mqtt(runtime_config, runtime_state, mock_context) -> None:
    comp = PinComponent(runtime_config, runtime_state, mock_context)  # type: ignore
    runtime_state.pending_analog_reads.append(PendingPinRequest(pin=3, reply_context=None))

    payload = structures.AnalogReadResponsePacket.SCHEMA.build({"pin": 3, "value": 512})

    await comp.handle_analog_read_resp(payload)

    mock_context.publish.assert_called_once()
    assert mock_context.publish.call_args[1]["payload"] == b"512"


@pytest.mark.asyncio
async def test_pin_mqtt_write_sends_serial_frame(runtime_config, runtime_state, mock_context) -> None:
    comp = PinComponent(runtime_config, runtime_state, mock_context)  # type: ignore

    # Simulate MQTT write to pin 5
    await comp.handle_mqtt(Topic.DIGITAL, "5", None, "1", None)

    mock_context.send_frame.assert_called_once()
    cmd_id, payload = mock_context.send_frame.call_args[0]
    assert cmd_id == Command.CMD_DIGITAL_WRITE.value
    # Verify pin 5 and value 1 are in payload
    decoded = structures.DigitalWritePacket.decode(payload)
    assert decoded.pin == 5
    assert decoded.value == protocol.DIGITAL_HIGH


@pytest.mark.asyncio
async def test_pin_mqtt_read_enqueues_and_sends_frame(runtime_config, runtime_state, mock_context) -> None:
    comp = PinComponent(runtime_config, runtime_state, mock_context)  # type: ignore

    await comp.handle_mqtt(Topic.DIGITAL, "13", "read", "", None)

    assert len(runtime_state.pending_digital_reads) == 1
    assert runtime_state.pending_digital_reads[0].pin == 13
    mock_context.send_frame.assert_called_with(Command.CMD_DIGITAL_READ.value, bytes([13]))


@pytest.mark.asyncio
async def test_system_free_memory_resp_publishes_to_mqtt(runtime_config, runtime_state, mock_context) -> None:
    comp = SystemComponent(runtime_config, runtime_state, mock_context)

    # [FIX] Use big-endian build for uint32
    payload = structures.UINT16_STRUCT.build(2048)
    await comp.handle_get_free_memory_resp(payload)

    mock_context.publish.assert_called_once()
    assert mock_context.publish.call_args[1]["payload"] == b"2048"


@pytest.mark.asyncio
async def test_system_version_resp_updates_state_and_mqtt(runtime_config, runtime_state, mock_context) -> None:
    comp = SystemComponent(runtime_config, runtime_state, mock_context)

    payload = bytes([2, 5]) # v2.5
    await comp.handle_get_version_resp(payload)

    assert runtime_state.mcu_version == (2, 5)
    mock_context.publish.assert_called()


@pytest.mark.asyncio
async def test_shell_mqtt_kill_calls_process_manager(runtime_config, runtime_state, mock_context) -> None:
    mock_process = MagicMock()
    # [FIX] Component calls stop_process, not handle_kill
    mock_process.stop_process = AsyncMock(return_value=True)

    comp = ShellComponent(runtime_config, runtime_state, mock_context, mock_process)

    await comp.handle_mqtt(["kill", "42"], b"", None)

    mock_process.stop_process.assert_called_once_with(42)
