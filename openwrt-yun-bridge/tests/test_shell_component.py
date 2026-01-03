"""Unit tests for yunbridge.services.components.shell."""

from __future__ import annotations

import struct
from collections.abc import Coroutine
from typing import Any, cast
from unittest.mock import AsyncMock

import pytest

from aiomqtt.message import Message as MQTTMessage

from yunbridge.config.settings import RuntimeConfig
from yunbridge.mqtt.messages import QueuedPublish
from yunbridge.policy import CommandValidationError
from yunbridge.protocol.topics import Topic, topic_path
from yunbridge.rpc import protocol
from yunbridge.rpc.protocol import ShellAction, Status
from yunbridge.services.components.shell import ShellComponent
from yunbridge.state.context import RuntimeState


class RecordingBridgeContext:
    def __init__(self, config: RuntimeConfig, state: RuntimeState) -> None:
        self.config = config
        self.state = state
        self.sent_frames: list[tuple[int, bytes]] = []
        self.enqueued: list[tuple[QueuedPublish, MQTTMessage | None]] = []

    async def send_frame(self, command_id: int, payload: bytes = b"") -> bool:
        self.sent_frames.append((command_id, payload))
        return True

    async def enqueue_mqtt(
        self, message: QueuedPublish, *, reply_context: MQTTMessage | None = None
    ) -> None:
        self.enqueued.append((message, reply_context))

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


def _fake_inbound() -> MQTTMessage:
    return cast(MQTTMessage, object())


@pytest.mark.asyncio
async def test_shell_run_ok_builds_text_response(
    runtime_config: RuntimeConfig,
    runtime_state: RuntimeState,
) -> None:
    ctx = RecordingBridgeContext(runtime_config, runtime_state)
    process = AsyncMock()
    process.run_sync.return_value = (Status.OK.value, b"hello\n", b"", 0)

    component = ShellComponent(runtime_config, runtime_state, ctx, process)

    inbound = _fake_inbound()
    await component.handle_mqtt(
        [runtime_state.mqtt_topic_prefix, Topic.SHELL.value, ShellAction.RUN.value],
        b"echo hello",
        inbound,
    )

    assert len(ctx.enqueued) == 1
    message, reply_context = ctx.enqueued[0]
    assert reply_context is inbound
    assert message.topic_name == topic_path(
        runtime_state.mqtt_topic_prefix,
        Topic.SHELL,
        protocol.MQTT_SUFFIX_RESPONSE,
    )
    text = message.payload.decode("utf-8", errors="ignore")
    assert "Exit Code" in text
    assert "-- STDOUT --" in text


@pytest.mark.asyncio
async def test_shell_run_timeout_message_mentions_configured_timeout(
    runtime_config: RuntimeConfig,
    runtime_state: RuntimeState,
) -> None:
    ctx = RecordingBridgeContext(runtime_config, runtime_state)
    process = AsyncMock()
    process.run_sync.return_value = (Status.TIMEOUT.value, b"", b"", None)

    component = ShellComponent(runtime_config, runtime_state, ctx, process)

    await component.handle_mqtt(
        [runtime_state.mqtt_topic_prefix, Topic.SHELL.value, ShellAction.RUN.value],
        b"sleep 1000",
        None,
    )

    text = ctx.enqueued[0][0].payload.decode("utf-8", errors="ignore")
    assert "timed out" in text
    assert str(runtime_state.process_timeout) in text


@pytest.mark.asyncio
async def test_shell_run_malformed_returns_empty_command_error(
    runtime_config: RuntimeConfig,
    runtime_state: RuntimeState,
) -> None:
    ctx = RecordingBridgeContext(runtime_config, runtime_state)
    process = AsyncMock()
    process.run_sync.return_value = (Status.MALFORMED.value, b"", b"", None)

    component = ShellComponent(runtime_config, runtime_state, ctx, process)

    await component.handle_mqtt(
        [runtime_state.mqtt_topic_prefix, Topic.SHELL.value, ShellAction.RUN.value],
        b"echo hi",
        None,
    )

    assert ctx.enqueued[0][0].payload.startswith(b"Error: Empty command")


@pytest.mark.asyncio
async def test_shell_run_error_uses_stderr_detail(
    runtime_config: RuntimeConfig,
    runtime_state: RuntimeState,
) -> None:
    ctx = RecordingBridgeContext(runtime_config, runtime_state)
    process = AsyncMock()
    process.run_sync.return_value = (Status.ERROR.value, b"", b"boom", 2)

    component = ShellComponent(runtime_config, runtime_state, ctx, process)

    await component.handle_mqtt(
        [runtime_state.mqtt_topic_prefix, Topic.SHELL.value, ShellAction.RUN.value],
        b"false",
        None,
    )

    assert ctx.enqueued[0][0].payload.startswith(b"Error: boom")


@pytest.mark.asyncio
async def test_shell_run_exception_triggers_fallback_publish(
    runtime_config: RuntimeConfig,
    runtime_state: RuntimeState,
) -> None:
    ctx = RecordingBridgeContext(runtime_config, runtime_state)
    process = AsyncMock()
    process.run_sync.side_effect = RuntimeError("crash")

    component = ShellComponent(runtime_config, runtime_state, ctx, process)

    inbound = _fake_inbound()
    with pytest.raises(RuntimeError):
        await component.handle_mqtt(
            [runtime_state.mqtt_topic_prefix, Topic.SHELL.value, ShellAction.RUN.value],
            b"echo hi",
            inbound,
        )

    assert len(ctx.enqueued) == 1
    message, reply_context = ctx.enqueued[0]
    assert reply_context is inbound
    assert b"shell handler failed unexpectedly" in message.payload


@pytest.mark.asyncio
async def test_shell_run_async_validation_error_publishes_error_topic(
    runtime_config: RuntimeConfig,
    runtime_state: RuntimeState,
) -> None:
    ctx = RecordingBridgeContext(runtime_config, runtime_state)
    process = AsyncMock()
    process.start_async.side_effect = CommandValidationError("bad")

    component = ShellComponent(runtime_config, runtime_state, ctx, process)

    inbound = _fake_inbound()
    await component.handle_mqtt(
        [
            runtime_state.mqtt_topic_prefix,
            Topic.SHELL.value,
            ShellAction.RUN_ASYNC.value,
        ],
        b"bad",
        inbound,
    )

    assert len(ctx.enqueued) == 1
    message, reply_context = ctx.enqueued[0]
    assert reply_context is inbound
    assert message.topic_name == topic_path(
        runtime_state.mqtt_topic_prefix,
        Topic.SHELL,
        ShellAction.RUN_ASYNC,
        "error",
    )
    assert message.payload == b"error:bad"


@pytest.mark.asyncio
async def test_shell_run_async_not_allowed_returns_error_payload(
    runtime_config: RuntimeConfig,
    runtime_state: RuntimeState,
) -> None:
    ctx = RecordingBridgeContext(runtime_config, runtime_state)
    process = AsyncMock()
    process.start_async.return_value = protocol.INVALID_ID_SENTINEL

    component = ShellComponent(runtime_config, runtime_state, ctx, process)

    await component.handle_mqtt(
        [
            runtime_state.mqtt_topic_prefix,
            Topic.SHELL.value,
            ShellAction.RUN_ASYNC.value,
        ],
        b"echo hi",
        None,
    )

    message = ctx.enqueued[0][0]
    assert message.payload == b"error:not_allowed"


@pytest.mark.asyncio
async def test_shell_poll_calls_process_helpers(
    runtime_config: RuntimeConfig,
    runtime_state: RuntimeState,
) -> None:
    ctx = RecordingBridgeContext(runtime_config, runtime_state)
    process = AsyncMock()
    process.collect_output.return_value = {"stdout": b"", "stderr": b""}

    component = ShellComponent(runtime_config, runtime_state, ctx, process)

    await component.handle_mqtt(
        [
            runtime_state.mqtt_topic_prefix,
            Topic.SHELL.value,
            ShellAction.POLL.value,
            "123",
        ],
        b"",
        None,
    )

    process.collect_output.assert_awaited_once_with(123)
    process.publish_poll_result.assert_awaited_once()


@pytest.mark.asyncio
async def test_shell_kill_packs_pid_and_suppresses_ack(
    runtime_config: RuntimeConfig,
    runtime_state: RuntimeState,
) -> None:
    ctx = RecordingBridgeContext(runtime_config, runtime_state)
    process = AsyncMock()

    component = ShellComponent(runtime_config, runtime_state, ctx, process)

    await component.handle_mqtt(
        [
            runtime_state.mqtt_topic_prefix,
            Topic.SHELL.value,
            ShellAction.KILL.value,
            "42",
        ],
        b"",
        None,
    )

    process.handle_kill.assert_awaited_once_with(
        struct.pack(protocol.UINT16_FORMAT, 42),
        send_ack=False,
    )


@pytest.mark.asyncio
async def test_shell_ignores_invalid_payloads_and_actions(
    runtime_config: RuntimeConfig,
    runtime_state: RuntimeState,
) -> None:
    ctx = RecordingBridgeContext(runtime_config, runtime_state)
    process = AsyncMock()

    component = ShellComponent(runtime_config, runtime_state, ctx, process)

    await component.handle_mqtt(
        [runtime_state.mqtt_topic_prefix, Topic.SHELL.value, ShellAction.RUN.value],
        b"",
        None,
    )
    await component.handle_mqtt(
        [
            runtime_state.mqtt_topic_prefix,
            Topic.SHELL.value,
            ShellAction.POLL.value,
            "0",
        ],
        b"",
        None,
    )
    await component.handle_mqtt(
        [runtime_state.mqtt_topic_prefix, Topic.SHELL.value, "unknown"],
        b"",
        None,
    )

    assert ctx.enqueued == []
    assert ctx.sent_frames == []
