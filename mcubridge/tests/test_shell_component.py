"""Unit tests for mcubridge.services.shell (SIL-2)."""

from __future__ import annotations

from typing import Any
from unittest.mock import AsyncMock

import pytest

from mcubridge.config.settings import RuntimeConfig
from mcubridge.protocol import protocol
from mcubridge.protocol.protocol import ShellAction, Status
from mcubridge.protocol.structures import QueuedPublish
from mcubridge.protocol.topics import Topic, topic_path
from mcubridge.services.process import ProcessComponent
from mcubridge.services.serial_flow import SerialFlowController
from mcubridge.state.context import RuntimeState
from mcubridge.transport.mqtt import MqttTransport
from tests._helpers import make_mqtt_msg, make_route


def _extract_enqueued_publish(
    mqtt_flow: AsyncMock, index: int = -1
) -> tuple[QueuedPublish, Any]:
    """Helper to extract QueuedPublish and reply_context from AsyncMock.publish calls."""
    call = mqtt_flow.publish.call_args_list[index]
    topic = call.kwargs.get("topic", call.args[0] if call.args else "")
    payload = call.kwargs.get("payload", call.args[1] if len(call.args) > 1 else b"")

    if isinstance(payload, str):
        payload = payload.encode("utf-8")

    msg = QueuedPublish(
        topic_name=topic,
        payload=payload,
        qos=call.kwargs.get("qos", 0),
        retain=call.kwargs.get("retain", False),
        content_type=call.kwargs.get("content_type"),
        message_expiry_interval=call.kwargs.get("expiry"),
        user_properties=call.kwargs.get("properties", ()),
    )
    return msg, call.kwargs.get("reply_to")


@pytest.mark.asyncio
async def test_shell_run_async_success(
    runtime_config: RuntimeConfig,
) -> None:
    # [SIL-2] Use AsyncMock(spec=Interface) for all component mocks
    state = AsyncMock(spec=RuntimeState)
    state.mqtt_topic_prefix = "br"

    mqtt_flow = AsyncMock(spec=MqttTransport)
    mqtt_flow.publish = AsyncMock()
    serial_flow = AsyncMock(spec=SerialFlowController)
    serial_flow.send = AsyncMock(return_value=True)

    component = ProcessComponent(
        config=runtime_config, state=state, serial_flow=serial_flow, mqtt_flow=mqtt_flow
    )

    # Mock low-level execution but use real component logic for MQTT
    component.run_async = AsyncMock(return_value=1234)

    inbound = make_mqtt_msg(b"echo hello")
    await component.handle_mqtt(
        make_route(Topic.SHELL, ShellAction.RUN_ASYNC.value),
        inbound,
    )

    mqtt_flow.publish.assert_awaited_once()
    msg, reply_to = _extract_enqueued_publish(mqtt_flow)
    assert reply_to is inbound
    assert msg.topic_name == topic_path(
        state.mqtt_topic_prefix,
        Topic.SHELL,
        ShellAction.RUN_ASYNC,
        protocol.MQTT_SUFFIX_RESPONSE,
    )
    assert msg.payload == b"1234"


@pytest.mark.asyncio
async def test_shell_run_async_exception_returns_error(
    runtime_config: RuntimeConfig,
) -> None:
    state = AsyncMock(spec=RuntimeState)
    state.mqtt_topic_prefix = "br"

    mqtt_flow = AsyncMock(spec=MqttTransport)
    mqtt_flow.publish = AsyncMock()
    serial_flow = AsyncMock(spec=SerialFlowController)
    serial_flow.send = AsyncMock(return_value=True)

    component = ProcessComponent(
        config=runtime_config, state=state, serial_flow=serial_flow, mqtt_flow=mqtt_flow
    )
    component.run_async = AsyncMock(side_effect=RuntimeError("crash"))

    inbound = make_mqtt_msg(b"echo hi")
    await component.handle_mqtt(
        make_route(Topic.SHELL, ShellAction.RUN_ASYNC.value),
        inbound,
    )

    mqtt_flow.publish.assert_awaited_once()
    msg, _ = _extract_enqueued_publish(mqtt_flow)
    assert msg.payload == b"error:internal"


@pytest.mark.asyncio
async def test_shell_run_async_not_allowed_returns_error_payload(
    runtime_config: RuntimeConfig,
) -> None:
    state = AsyncMock(spec=RuntimeState)
    state.mqtt_topic_prefix = "br"

    mqtt_flow = AsyncMock(spec=MqttTransport)
    mqtt_flow.publish = AsyncMock()
    serial_flow = AsyncMock(spec=SerialFlowController)
    serial_flow.send = AsyncMock(return_value=True)

    component = ProcessComponent(
        config=runtime_config, state=state, serial_flow=serial_flow, mqtt_flow=mqtt_flow
    )
    component.run_async = AsyncMock(return_value=0)

    await component.handle_mqtt(
        make_route(Topic.SHELL, ShellAction.RUN_ASYNC.value),
        make_mqtt_msg(b"echo hi"),
    )

    mqtt_flow.publish.assert_awaited_once()
    msg, _ = _extract_enqueued_publish(mqtt_flow)
    assert msg.payload == b"error:not_allowed_or_limit_reached"


@pytest.mark.asyncio
async def test_shell_poll_calls_process_helpers(
    runtime_config: RuntimeConfig,
) -> None:
    state = AsyncMock(spec=RuntimeState)
    state.mqtt_topic_prefix = "br"

    mqtt_flow = AsyncMock(spec=MqttTransport)
    mqtt_flow.publish = AsyncMock()
    serial_flow = AsyncMock(spec=SerialFlowController)
    serial_flow.send = AsyncMock(return_value=True)

    component = ProcessComponent(
        config=runtime_config, state=state, serial_flow=serial_flow, mqtt_flow=mqtt_flow
    )

    from mcubridge.protocol.structures import ProcessOutputBatch

    batch = ProcessOutputBatch(Status.OK.value, 0, b"", b"", True, False, False)
    component.poll_process = AsyncMock(return_value=batch)
    component.publish_poll_result = AsyncMock()

    inbound = make_mqtt_msg(b"")
    await component.handle_mqtt(
        make_route(Topic.SHELL, ShellAction.POLL.value, "123"),
        inbound,
    )

    component.poll_process.assert_awaited_once_with(123)
    component.publish_poll_result.assert_awaited_once_with(123, batch, inbound)


@pytest.mark.asyncio
async def test_shell_kill_invokes_stop_process(
    runtime_config: RuntimeConfig,
) -> None:
    state = AsyncMock(spec=RuntimeState)
    state.mqtt_topic_prefix = "br"

    mqtt_flow = AsyncMock(spec=MqttTransport)
    mqtt_flow.publish = AsyncMock()
    serial_flow = AsyncMock(spec=SerialFlowController)
    serial_flow.send = AsyncMock(return_value=True)

    component = ProcessComponent(
        config=runtime_config, state=state, serial_flow=serial_flow, mqtt_flow=mqtt_flow
    )
    component.stop_process = AsyncMock(return_value=True)

    await component.handle_mqtt(
        make_route(Topic.SHELL, ShellAction.KILL.value, "42"),
        make_mqtt_msg(b""),
    )

    component.stop_process.assert_awaited_once_with(42)


@pytest.mark.asyncio
async def test_shell_ignores_invalid_payloads_and_actions(
    runtime_config: RuntimeConfig,
) -> None:
    state = AsyncMock(spec=RuntimeState)

    mqtt_flow = AsyncMock(spec=MqttTransport)
    mqtt_flow.publish = AsyncMock()
    serial_flow = AsyncMock(spec=SerialFlowController)
    serial_flow.send = AsyncMock(return_value=True)

    component = ProcessComponent(
        config=runtime_config, state=state, serial_flow=serial_flow, mqtt_flow=mqtt_flow
    )

    # Empty segments
    await component.handle_mqtt(make_route(Topic.SHELL), make_mqtt_msg(b""))

    # Unknown action
    await component.handle_mqtt(make_route(Topic.SHELL, "unknown"), make_mqtt_msg(b""))

    mqtt_flow.publish.assert_not_called()
    serial_flow.send.assert_not_called()
