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
from mcubridge.services.base import BridgeContext
from mcubridge.services.process import ProcessComponent
from mcubridge.state.context import RuntimeState
from tests._helpers import make_mqtt_msg, make_route


def _extract_enqueued_publish(ctx: AsyncMock, index: int = -1) -> tuple[QueuedPublish, Any]:
    """Helper to extract QueuedPublish and reply_context from AsyncMock.publish calls."""
    call = ctx.publish.call_args_list[index]
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
    runtime_state: RuntimeState,
) -> None:
    ctx = AsyncMock(spec=BridgeContext)
    ctx.config = runtime_config
    ctx.state = runtime_state
    ctx.send_frame.return_value = True

    component = ProcessComponent(runtime_config, runtime_state, ctx)

    # Mock low-level execution but use real component logic for MQTT
    component.run_async = AsyncMock(return_value=1234)

    inbound = make_mqtt_msg(b"echo hello")
    await component.handle_mqtt(
        make_route(Topic.SHELL, ShellAction.RUN_ASYNC.value),
        inbound,
    )

    assert ctx.publish.call_count == 1
    msg, reply_to = _extract_enqueued_publish(ctx)
    assert reply_to is inbound
    assert msg.topic_name == topic_path(
        runtime_state.mqtt_topic_prefix,
        Topic.SHELL,
        ShellAction.RUN_ASYNC,
        protocol.MQTT_SUFFIX_RESPONSE,
    )
    assert msg.payload == b"1234"


@pytest.mark.asyncio
async def test_shell_run_async_exception_returns_error(
    runtime_config: RuntimeConfig,
    runtime_state: RuntimeState,
) -> None:
    ctx = AsyncMock(spec=BridgeContext)
    ctx.config = runtime_config
    ctx.state = runtime_state
    ctx.send_frame.return_value = True

    component = ProcessComponent(runtime_config, runtime_state, ctx)
    component.run_async = AsyncMock(side_effect=RuntimeError("crash"))

    inbound = make_mqtt_msg(b"echo hi")
    await component.handle_mqtt(
        make_route(Topic.SHELL, ShellAction.RUN_ASYNC.value),
        inbound,
    )

    assert ctx.publish.call_count == 1
    msg, _ = _extract_enqueued_publish(ctx)
    assert msg.payload == b"error:internal"


@pytest.mark.asyncio
async def test_shell_run_async_not_allowed_returns_error_payload(
    runtime_config: RuntimeConfig,
    runtime_state: RuntimeState,
) -> None:
    ctx = AsyncMock(spec=BridgeContext)
    ctx.config = runtime_config
    ctx.state = runtime_state
    ctx.send_frame.return_value = True

    component = ProcessComponent(runtime_config, runtime_state, ctx)
    component.run_async = AsyncMock(return_value=0)

    await component.handle_mqtt(
        make_route(Topic.SHELL, ShellAction.RUN_ASYNC.value),
        make_mqtt_msg(b"echo hi"),
    )

    assert ctx.publish.call_count == 1
    msg, _ = _extract_enqueued_publish(ctx)
    assert msg.payload == b"error:not_allowed_or_limit_reached"


@pytest.mark.asyncio
async def test_shell_poll_calls_process_helpers(
    runtime_config: RuntimeConfig,
    runtime_state: RuntimeState,
) -> None:
    ctx = AsyncMock(spec=BridgeContext)
    ctx.config = runtime_config
    ctx.state = runtime_state
    ctx.send_frame.return_value = True

    component = ProcessComponent(runtime_config, runtime_state, ctx)

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
    runtime_state: RuntimeState,
) -> None:
    ctx = AsyncMock(spec=BridgeContext)
    ctx.config = runtime_config
    ctx.state = runtime_state
    ctx.send_frame.return_value = True

    component = ProcessComponent(runtime_config, runtime_state, ctx)
    component.stop_process = AsyncMock(return_value=True)

    await component.handle_mqtt(
        make_route(Topic.SHELL, ShellAction.KILL.value, "42"),
        make_mqtt_msg(b""),
    )

    component.stop_process.assert_awaited_once_with(42)


@pytest.mark.asyncio
async def test_shell_ignores_invalid_payloads_and_actions(
    runtime_config: RuntimeConfig,
    runtime_state: RuntimeState,
) -> None:
    ctx = AsyncMock(spec=BridgeContext)
    ctx.config = runtime_config
    ctx.state = runtime_state
    ctx.send_frame.return_value = True

    component = ProcessComponent(runtime_config, runtime_state, ctx)

    # Empty segments
    await component.handle_mqtt(make_route(Topic.SHELL), make_mqtt_msg(b""))

    # Unknown action
    await component.handle_mqtt(make_route(Topic.SHELL, "unknown"), make_mqtt_msg(b""))

    ctx.publish.assert_not_called()
    ctx.send_frame.assert_not_called()
