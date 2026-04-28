"""Unit tests for mcubridge.services.shell (SIL-2)."""

from __future__ import annotations

from typing import Any
from unittest.mock import AsyncMock

import pytest

from mcubridge.config.settings import RuntimeConfig
from mcubridge.protocol import protocol
from mcubridge.protocol.protocol import ShellAction, Status
from mcubridge.protocol.structures import QueuedPublish, TopicRoute
from mcubridge.protocol.topics import Topic, topic_path
from mcubridge.services.process import ProcessComponent
from mcubridge.services.serial_flow import SerialFlowController
from mcubridge.state.context import RuntimeState
from mcubridge.transport.mqtt import MqttTransport
from aiomqtt.message import Message


def _extract_enqueued_publish(
    mqtt_flow: AsyncMock, index: int = -1
) -> tuple[QueuedPublish, Any]:
    """Helper to extract QueuedPublish and reply_context from AsyncMock.enqueue_mqtt calls."""
    call = mqtt_flow.enqueue_mqtt.call_args_list[index]
    msg = call.args[0] if call.args else call.kwargs.get("message")
    reply_to = call.kwargs.get("reply_context")
    return msg, reply_to


@pytest.mark.asyncio
async def test_shell_run_async_success(
    runtime_config: RuntimeConfig,
) -> None:
    # [SIL-2] Use AsyncMock(spec=Interface) for all component mocks
    state = AsyncMock(spec=RuntimeState)
    state.mqtt_topic_prefix = "br"

    mqtt_flow = AsyncMock(spec=MqttTransport)
    mqtt_flow.enqueue_mqtt = AsyncMock()
    serial_flow = AsyncMock(spec=SerialFlowController)
    serial_flow.send = AsyncMock(return_value=True)

    component = ProcessComponent(
        config=runtime_config, state=state, serial_flow=serial_flow, mqtt_flow=mqtt_flow
    )

    # Mock low-level execution but use real component logic for MQTT
    component.run_async = AsyncMock(return_value=1234)

    inbound = Message(
        topic="test/topic",
        payload=b"echo hello",
        qos=0,
        retain=False,
        mid=1,
        properties=None,
    )
    await component.handle_mqtt(
        TopicRoute(
            raw=f"br/{Topic.SHELL}/{ShellAction.RUN_ASYNC.value}",
            prefix="br",
            topic=Topic.SHELL,
            segments=(ShellAction.RUN_ASYNC.value,),
        ),
        inbound,
    )

    mqtt_flow.enqueue_mqtt.assert_awaited_once()
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
    mqtt_flow.enqueue_mqtt = AsyncMock()
    serial_flow = AsyncMock(spec=SerialFlowController)
    serial_flow.send = AsyncMock(return_value=True)

    component = ProcessComponent(
        config=runtime_config, state=state, serial_flow=serial_flow, mqtt_flow=mqtt_flow
    )
    component.run_async = AsyncMock(side_effect=RuntimeError("crash"))

    inbound = Message(
        topic="test/topic",
        payload=b"echo hi",
        qos=0,
        retain=False,
        mid=1,
        properties=None,
    )
    await component.handle_mqtt(
        TopicRoute(
            raw=f"br/{Topic.SHELL}/{ShellAction.RUN_ASYNC.value}",
            prefix="br",
            topic=Topic.SHELL,
            segments=(ShellAction.RUN_ASYNC.value,),
        ),
        inbound,
    )

    mqtt_flow.enqueue_mqtt.assert_awaited_once()
    msg, _ = _extract_enqueued_publish(mqtt_flow)
    assert msg.payload == b"error:internal"


@pytest.mark.asyncio
async def test_shell_run_async_not_allowed_returns_error_payload(
    runtime_config: RuntimeConfig,
) -> None:
    state = AsyncMock(spec=RuntimeState)
    state.mqtt_topic_prefix = "br"

    mqtt_flow = AsyncMock(spec=MqttTransport)
    mqtt_flow.enqueue_mqtt = AsyncMock()
    serial_flow = AsyncMock(spec=SerialFlowController)
    serial_flow.send = AsyncMock(return_value=True)

    component = ProcessComponent(
        config=runtime_config, state=state, serial_flow=serial_flow, mqtt_flow=mqtt_flow
    )
    component.run_async = AsyncMock(return_value=0)
    await component.handle_mqtt(
        TopicRoute(
            raw=f"br/{Topic.SHELL}/{ShellAction.RUN_ASYNC.value}",
            prefix="br",
            topic=Topic.SHELL,
            segments=(ShellAction.RUN_ASYNC.value,),
        ),
        Message(
            topic="test/topic",
            payload=b"echo hi",
            qos=0,
            retain=False,
            mid=1,
            properties=None,
        ),
    )
    mqtt_flow.enqueue_mqtt.assert_awaited_once()
    msg, _ = _extract_enqueued_publish(mqtt_flow)
    assert msg.payload == b"error:not_allowed_or_limit_reached"


@pytest.mark.asyncio
async def test_shell_poll_calls_process_helpers(
    runtime_config: RuntimeConfig,
) -> None:
    state = AsyncMock(spec=RuntimeState)
    state.mqtt_topic_prefix = "br"

    mqtt_flow = AsyncMock(spec=MqttTransport)
    mqtt_flow.enqueue_mqtt = AsyncMock()
    serial_flow = AsyncMock(spec=SerialFlowController)
    serial_flow.send = AsyncMock(return_value=True)

    component = ProcessComponent(
        config=runtime_config, state=state, serial_flow=serial_flow, mqtt_flow=mqtt_flow
    )

    from mcubridge.protocol.structures import ProcessOutputBatch

    batch = ProcessOutputBatch(Status.OK.value, 0, b"", b"", True, False, False)
    component.poll_process = AsyncMock(return_value=batch)
    component.publish_poll_result = AsyncMock()

    inbound = Message(
        topic="test/topic", payload=b"", qos=0, retain=False, mid=1, properties=None
    )
    await component.handle_mqtt(
        TopicRoute("br/shell/poll/123", "br", Topic.SHELL, ("poll", "123")),
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
    mqtt_flow.enqueue_mqtt = AsyncMock()
    serial_flow = AsyncMock(spec=SerialFlowController)
    serial_flow.send = AsyncMock(return_value=True)

    component = ProcessComponent(
        config=runtime_config, state=state, serial_flow=serial_flow, mqtt_flow=mqtt_flow
    )
    component.stop_process = AsyncMock(return_value=True)

    await component.handle_mqtt(
        TopicRoute("br/shell/kill/42", "br", Topic.SHELL, ("kill", "42")),
        Message(
            topic="test/topic", payload=b"", qos=0, retain=False, mid=1, properties=None
        ),
    )

    component.stop_process.assert_awaited_once_with(42)


@pytest.mark.asyncio
async def test_shell_ignores_invalid_payloads_and_actions(
    runtime_config: RuntimeConfig,
) -> None:
    state = AsyncMock(spec=RuntimeState)

    mqtt_flow = AsyncMock(spec=MqttTransport)
    mqtt_flow.enqueue_mqtt = AsyncMock()
    serial_flow = AsyncMock(spec=SerialFlowController)
    serial_flow.send = AsyncMock(return_value=True)

    component = ProcessComponent(
        config=runtime_config, state=state, serial_flow=serial_flow, mqtt_flow=mqtt_flow
    )

    # Empty segments
    await component.handle_mqtt(
        TopicRoute("br/shell", "br", Topic.SHELL, ()),
        Message(
            topic="test/topic", payload=b"", qos=0, retain=False, mid=1, properties=None
        ),
    )

    # Unknown action
    await component.handle_mqtt(
        TopicRoute("br/shell/unknown", "br", Topic.SHELL, ("unknown",)),
        Message(
            topic="test/topic", payload=b"", qos=0, retain=False, mid=1, properties=None
        ),
    )

    mqtt_flow.enqueue_mqtt.assert_not_called()
    serial_flow.send.assert_not_called()
