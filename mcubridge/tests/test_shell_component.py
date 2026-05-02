"""Unit tests for mcubridge.services.shell (SIL-2)."""

from __future__ import annotations

from typing import Any, cast
from unittest.mock import AsyncMock

import pytest
from aiomqtt.message import Message

from mcubridge.config.settings import RuntimeConfig
from mcubridge.protocol import protocol
from mcubridge.protocol.protocol import ShellAction
from mcubridge.protocol.structures import QueuedPublish, TopicRoute
from mcubridge.protocol.topics import Topic, topic_path
from mcubridge.services.process import ProcessComponent
from mcubridge.services.serial_flow import SerialFlowController
from mcubridge.state.context import RuntimeState


def _extract_enqueued_publish(
    enqueue_mqtt: AsyncMock, index: int = -1
) -> tuple[QueuedPublish, Any]:
    """Helper to extract QueuedPublish and reply_context from AsyncMock calls."""
    call = enqueue_mqtt.call_args_list[index]
    msg = call.args[0] if call.args else call.kwargs.get("message")
    reply_to = call.kwargs.get("reply_context")
    return cast(QueuedPublish, msg), reply_to


@pytest.mark.asyncio
async def test_shell_run_async_success(
    runtime_config: RuntimeConfig,
) -> None:
    state = AsyncMock(spec=RuntimeState)
    state.mqtt_topic_prefix = "br"

    enqueue_mqtt = AsyncMock()
    serial_flow = AsyncMock(spec=SerialFlowController)
    serial_flow.send = AsyncMock(return_value=True)

    component = ProcessComponent(
        config=runtime_config,
        state=state,
        serial_flow=serial_flow,
        enqueue_mqtt=enqueue_mqtt,
    )

    # Mock high-level method
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

    enqueue_mqtt.assert_called_once()
    msg, reply_to = _extract_enqueued_publish(enqueue_mqtt)
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

    enqueue_mqtt = AsyncMock()
    serial_flow = AsyncMock(spec=SerialFlowController)
    serial_flow.send = AsyncMock(return_value=True)

    component = ProcessComponent(
        config=runtime_config,
        state=state,
        serial_flow=serial_flow,
        enqueue_mqtt=enqueue_mqtt,
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

    enqueue_mqtt.assert_called_once()
    msg, _ = _extract_enqueued_publish(enqueue_mqtt)
    assert msg.payload == b"error:internal"


@pytest.mark.asyncio
async def test_shell_run_async_not_allowed_returns_error_payload(
    runtime_config: RuntimeConfig,
) -> None:
    state = AsyncMock(spec=RuntimeState)
    state.mqtt_topic_prefix = "br"

    enqueue_mqtt = AsyncMock()
    serial_flow = AsyncMock(spec=SerialFlowController)
    serial_flow.send = AsyncMock(return_value=True)

    component = ProcessComponent(
        config=runtime_config,
        state=state,
        serial_flow=serial_flow,
        enqueue_mqtt=enqueue_mqtt,
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
    enqueue_mqtt.assert_called_once()
    msg, _ = _extract_enqueued_publish(enqueue_mqtt)
    assert msg.payload == b"error:not_allowed_or_limit_reached"


@pytest.mark.asyncio
async def test_shell_kill_invokes_stop_process(
    runtime_config: RuntimeConfig,
) -> None:
    state = AsyncMock(spec=RuntimeState)
    state.mqtt_topic_prefix = "br"

    enqueue_mqtt = AsyncMock()
    serial_flow = AsyncMock(spec=SerialFlowController)
    serial_flow.send = AsyncMock(return_value=True)

    component = ProcessComponent(
        config=runtime_config,
        state=state,
        serial_flow=serial_flow,
        enqueue_mqtt=enqueue_mqtt,
    )
    component.stop_process = AsyncMock(return_value=True)

    await component.handle_mqtt(
        TopicRoute("br/shell/kill", "br", Topic.SHELL, ("kill",)),
        Message(
            topic="test/topic",
            payload=b"42",
            qos=0,
            retain=False,
            mid=1,
            properties=None,
        ),
    )

    component.stop_process.assert_awaited_once_with(42)


@pytest.mark.asyncio
async def test_shell_ignores_invalid_payloads_and_actions(
    runtime_config: RuntimeConfig,
) -> None:
    state = AsyncMock(spec=RuntimeState)

    enqueue_mqtt = AsyncMock()
    serial_flow = AsyncMock(spec=SerialFlowController)
    serial_flow.send = AsyncMock(return_value=True)

    component = ProcessComponent(
        config=runtime_config,
        state=state,
        serial_flow=serial_flow,
        enqueue_mqtt=enqueue_mqtt,
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

    enqueue_mqtt.assert_not_called()
    serial_flow.send.assert_not_called()
