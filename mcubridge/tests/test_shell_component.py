"""Unit tests for mcubridge.services.shell."""

from __future__ import annotations

from collections.abc import Coroutine
from typing import Any
from unittest.mock import AsyncMock

import pytest
from aiomqtt.message import Message
from mcubridge.config.settings import RuntimeConfig
from mcubridge.protocol import protocol
from mcubridge.protocol.protocol import ShellAction
from mcubridge.protocol.structures import QueuedPublish
from mcubridge.protocol.topics import Topic, topic_path
from mcubridge.services.process import ProcessComponent
from mcubridge.state.context import RuntimeState
from tests._helpers import make_mqtt_msg, make_route


class RecordingBridgeContext:
    def __init__(self, config: RuntimeConfig, state: RuntimeState) -> None:
        self.config = config
        self.state = state
        self.sent_frames: list[tuple[int, bytes]] = []
        self.enqueued: list[tuple[QueuedPublish, Message | None]] = []

    async def send_frame(self, command_id: int, payload: bytes = b"") -> bool:
        self.sent_frames.append((command_id, payload))
        return True

    async def publish(
        self,
        topic: str,
        payload: bytes | str,
        *,
        qos: int = 0,
        retain: bool = False,
        expiry: int | None = None,
        properties: tuple[tuple[str, str], ...] = (),
        content_type: str | None = None,
        reply_to: Message | None = None,
    ) -> None:
        if isinstance(payload, str):
            payload_bytes = payload.encode("utf-8")
        else:
            payload_bytes = payload

        message = QueuedPublish(
            topic_name=topic,
            payload=payload_bytes,
            qos=qos,
            retain=retain,
            content_type=content_type,
            message_expiry_interval=expiry,
            user_properties=list(properties or []),
        )
        self.enqueued.append((message, reply_to))

    async def enqueue_mqtt(
        self, message: QueuedPublish, *, reply_context: Message | None = None
    ) -> None:
        self.enqueued.append((message, reply_context))

    async def acknowledge_mcu_frame(
        self, command_id: int, seq_id: int, *, status: Any = None
    ) -> None:
        pass

    def is_command_allowed(self, command: str) -> bool:
        return True

    async def schedule_background(
        self,
        coroutine: Coroutine[Any, Any, None],
        *,
        name: str | None = None,
    ) -> Any:
        task = AsyncMock()
        await coroutine
        return task


@pytest.mark.asyncio
async def test_shell_run_async_success(
    runtime_config: RuntimeConfig,
    runtime_state: RuntimeState,
) -> None:
    ctx = RecordingBridgeContext(runtime_config, runtime_state)
    component = ProcessComponent(runtime_config, runtime_state, ctx)

    # Mock low-level execution but use real component logic for MQTT
    component.run_async = AsyncMock(return_value=1234)

    inbound = make_mqtt_msg(b"echo hello")
    await component.handle_mqtt(
        make_route(Topic.SHELL, ShellAction.RUN_ASYNC.value),
        inbound,
    )

    assert len(ctx.enqueued) == 1
    message, reply_context = ctx.enqueued[0]
    assert reply_context is inbound
    assert message.topic_name == topic_path(
        runtime_state.mqtt_topic_prefix,
        Topic.SHELL,
        ShellAction.RUN_ASYNC,
        protocol.MQTT_SUFFIX_RESPONSE,
    )
    assert message.payload == b"1234"


@pytest.mark.asyncio
async def test_shell_run_async_exception_returns_error(
    runtime_config: RuntimeConfig,
    runtime_state: RuntimeState,
) -> None:
    ctx = RecordingBridgeContext(runtime_config, runtime_state)
    component = ProcessComponent(runtime_config, runtime_state, ctx)

    component.run_async = AsyncMock(side_effect=RuntimeError("crash"))

    inbound = make_mqtt_msg(b"echo hi")
    await component.handle_mqtt(
        make_route(Topic.SHELL, ShellAction.RUN_ASYNC.value),
        inbound,
    )

    assert len(ctx.enqueued) == 1
    message, _ = ctx.enqueued[0]
    assert message.payload == b"error:internal"


@pytest.mark.asyncio
async def test_shell_run_async_not_allowed_returns_error_payload(
    runtime_config: RuntimeConfig,
    runtime_state: RuntimeState,
) -> None:
    ctx = RecordingBridgeContext(runtime_config, runtime_state)
    component = ProcessComponent(runtime_config, runtime_state, ctx)

    component.run_async = AsyncMock(return_value=0)

    await component.handle_mqtt(
        make_route(Topic.SHELL, ShellAction.RUN_ASYNC.value),
        make_mqtt_msg(b"echo hi"),
    )

    message = ctx.enqueued[0][0]
    assert message.payload == b"error:not_allowed_or_limit_reached"


@pytest.mark.asyncio
async def test_shell_poll_calls_process_helpers(
    runtime_config: RuntimeConfig,
    runtime_state: RuntimeState,
) -> None:
    ctx = RecordingBridgeContext(runtime_config, runtime_state)
    component = ProcessComponent(runtime_config, runtime_state, ctx)

    from mcubridge.protocol.protocol import Status
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
    ctx = RecordingBridgeContext(runtime_config, runtime_state)
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
    ctx = RecordingBridgeContext(runtime_config, runtime_state)
    component = ProcessComponent(runtime_config, runtime_state, ctx)

    # Empty segments
    await component.handle_mqtt(make_route(Topic.SHELL), make_mqtt_msg(b""))

    # Unknown action
    await component.handle_mqtt(make_route(Topic.SHELL, "unknown"), make_mqtt_msg(b""))

    assert ctx.enqueued == []
    assert ctx.sent_frames == []
