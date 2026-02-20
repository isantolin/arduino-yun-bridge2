"""Unit tests for the SystemComponent publishing logic."""

from __future__ import annotations

import asyncio
from collections.abc import Coroutine
from typing import Any

import pytest
from aiomqtt.message import Message
from mcubridge.config.settings import RuntimeConfig
from mcubridge.mqtt.messages import QueuedPublish
from mcubridge.protocol import protocol, structures
from mcubridge.protocol.protocol import Command
from mcubridge.protocol.topics import Topic, topic_path
from mcubridge.services.system import SystemComponent
from mcubridge.state.context import RuntimeState

from .mqtt_helpers import make_inbound_message


class DummyContext:
    def __init__(self, config: RuntimeConfig, state: RuntimeState) -> None:
        self.config = config
        self.state = state
        self.sent_frames: list[tuple[int, bytes]] = []
        self.published: list[tuple[QueuedPublish, Message | None]] = []
        self.scheduled: list[Coroutine[Any, Any, None]] = []
        self.send_result: bool = True

    async def send_frame(self, command_id: int, payload: bytes = b"") -> bool:
        self.sent_frames.append((command_id, payload))
        return self.send_result

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
            user_properties=properties,
        )
        self.published.append((message, reply_to))

    async def enqueue_mqtt(
        self,
        message: QueuedPublish,
        *,
        reply_context: Message | None = None,
    ) -> None:
        self.published.append((message, reply_context))

    def is_command_allowed(self, command: str) -> bool:
        return True

    async def schedule_background(
        self,
        coroutine: Coroutine[Any, Any, None],
        *,
        name: str | None = None,
    ) -> asyncio.Task[Any]:
        self.scheduled.append(coroutine)
        return asyncio.create_task(coroutine, name=name)


def _run(coro: Coroutine[Any, Any, None]) -> None:
    asyncio.run(coro)


def _make_inbound(topic: str) -> Message:
    return make_inbound_message(
        topic,
        response_topic=f"reply/{topic}",
        correlation_data=b"cid",
    )


def test_request_mcu_version_resets_cached_version(
    runtime_config: RuntimeConfig,
    runtime_state: RuntimeState,
) -> None:
    async def _coro() -> None:
        ctx = DummyContext(runtime_config, runtime_state)
        component = SystemComponent(runtime_config, runtime_state, ctx)

        runtime_state.mcu_version = (3, 4)
        ok = await component.request_mcu_version()
        assert ok is True
        assert ctx.sent_frames == [(Command.CMD_GET_VERSION.value, b"")]
        assert runtime_state.mcu_version is None

        runtime_state.mcu_version = (5, 6)
        ctx.send_result = False
        ok = await component.request_mcu_version()
        assert ok is False
        assert runtime_state.mcu_version == (5, 6)

    _run(_coro())


def test_handle_get_free_memory_resp_publishes_with_pending_reply(
    runtime_config: RuntimeConfig,
    runtime_state: RuntimeState,
) -> None:
    async def _coro() -> None:
        ctx = DummyContext(runtime_config, runtime_state)
        component = SystemComponent(runtime_config, runtime_state, ctx)

        inbound = _make_inbound("free")
        # pyright: ignore[reportPrivateUsage]
        component._pending_free_memory.append(inbound)

        await component.handle_get_free_memory_resp(structures.UINT16_STRUCT.build(100))

        assert len(ctx.published) == 2
        message, reply_context = ctx.published[0]
        assert reply_context is inbound
        assert message.payload == b"100"
        assert message.content_type == "text/plain; charset=utf-8"
        assert message.message_expiry_interval == 10
        expected_topic = topic_path(
            runtime_state.mqtt_topic_prefix,
            Topic.SYSTEM,
            "free_memory",
            "value",
        )
        assert message.topic_name == expected_topic
        # pyright: ignore[reportPrivateUsage]
        assert not component._pending_free_memory

    _run(_coro())


def test_handle_get_free_memory_resp_ignores_malformed(
    runtime_config: RuntimeConfig,
    runtime_state: RuntimeState,
    caplog: pytest.LogCaptureFixture,
) -> None:
    async def _coro() -> None:
        ctx = DummyContext(runtime_config, runtime_state)
        component = SystemComponent(runtime_config, runtime_state, ctx)

        caplog.set_level("WARNING", logger="mcubridge.system")

        await component.handle_get_free_memory_resp(structures.UINT8_STRUCT.build(protocol.DIGITAL_HIGH))

        assert not ctx.published
        # pyright: ignore[reportPrivateUsage]
        assert not component._pending_free_memory
        assert any(
            "Malformed GET_FREE_MEMORY_RESP" in message
            for message in (record.getMessage() for record in caplog.records)
        )

    _run(_coro())


def test_handle_get_version_resp_publishes_pending_and_updates_state(
    runtime_config: RuntimeConfig,
    runtime_state: RuntimeState,
) -> None:
    async def _coro() -> None:
        ctx = DummyContext(runtime_config, runtime_state)
        component = SystemComponent(runtime_config, runtime_state, ctx)

        inbound = _make_inbound("version")
        # pyright: ignore[reportPrivateUsage]
        component._pending_version.append(inbound)

        await component.handle_get_version_resp(bytes([1, 2]))

        assert runtime_state.mcu_version == (1, 2)
        assert len(ctx.published) == 2
        message, reply_ctx = ctx.published[0]
        assert reply_ctx is inbound
        assert message.payload == b"1.2"
        assert message.message_expiry_interval == 60
        assert message.content_type == "text/plain; charset=utf-8"
        expected_topic = topic_path(
            runtime_state.mqtt_topic_prefix,
            Topic.SYSTEM,
            "version",
            "value",
        )
        assert message.topic_name == expected_topic
        # pyright: ignore[reportPrivateUsage]
        assert not component._pending_version

    _run(_coro())


def test_handle_get_version_resp_malformed(
    runtime_config: RuntimeConfig,
    runtime_state: RuntimeState,
    caplog: pytest.LogCaptureFixture,
) -> None:
    async def _coro() -> None:
        ctx = DummyContext(runtime_config, runtime_state)
        component = SystemComponent(runtime_config, runtime_state, ctx)

        caplog.set_level("WARNING", logger="mcubridge.system")

        await component.handle_get_version_resp(b"bad")

        assert runtime_state.mcu_version is None
        assert not ctx.published
        assert any(
            "Malformed GET_VERSION_RESP" in message for message in (record.getMessage() for record in caplog.records)
        )

    _run(_coro())


def test_handle_mqtt_version_get_with_cached_version(
    runtime_config: RuntimeConfig,
    runtime_state: RuntimeState,
) -> None:
    async def _coro() -> None:
        ctx = DummyContext(runtime_config, runtime_state)
        component = SystemComponent(runtime_config, runtime_state, ctx)

        runtime_state.mcu_version = (2, 5)
        inbound = _make_inbound(
            topic_path(
                runtime_state.mqtt_topic_prefix,
                Topic.SYSTEM,
                "version",
                "get",
            )
        )

        handled = await component.handle_mqtt(
            "version",
            ["get"],
            inbound,
        )

        assert handled is True
        assert ctx.sent_frames == [(Command.CMD_GET_VERSION.value, b"")]
        assert runtime_state.mcu_version is None
        # pyright: ignore[reportPrivateUsage]
        assert not component._pending_version
        assert len(ctx.published) == 3
        assert ctx.published[0][1] is inbound
        assert ctx.published[1][1] is None
        assert ctx.published[2][1] is None
        assert ctx.published[0][0].payload == b"2.5"

    _run(_coro())


def test_handle_mqtt_version_get_without_cached_version(
    runtime_config: RuntimeConfig,
    runtime_state: RuntimeState,
) -> None:
    async def _coro() -> None:
        ctx = DummyContext(runtime_config, runtime_state)
        component = SystemComponent(runtime_config, runtime_state, ctx)

        inbound = _make_inbound(
            topic_path(
                runtime_state.mqtt_topic_prefix,
                Topic.SYSTEM,
                "version",
                "get",
            )
        )

        handled = await component.handle_mqtt(
            "version",
            ["get"],
            inbound,
        )

        assert handled is True
        assert ctx.sent_frames == [(Command.CMD_GET_VERSION.value, b"")]
        assert component._pending_version
        # pyright: ignore[reportPrivateUsage]
        first_pending_version = component._pending_version[0]
        assert first_pending_version is inbound
        assert not ctx.published

    _run(_coro())


def test_handle_mqtt_free_memory_get_tracks_pending(
    runtime_config: RuntimeConfig,
    runtime_state: RuntimeState,
) -> None:
    async def _coro() -> None:
        ctx = DummyContext(runtime_config, runtime_state)
        component = SystemComponent(runtime_config, runtime_state, ctx)

        inbound = _make_inbound(
            topic_path(
                runtime_state.mqtt_topic_prefix,
                Topic.SYSTEM,
                "free_memory",
                "get",
            )
        )

        handled = await component.handle_mqtt(
            "free_memory",
            ["get"],
            inbound,
        )

        assert handled is True
        assert ctx.sent_frames == [(Command.CMD_GET_FREE_MEMORY.value, b"")]
        assert component._pending_free_memory
        # pyright: ignore[reportPrivateUsage]
        first_pending_free = component._pending_free_memory[0]
        assert first_pending_free is inbound
        assert not ctx.published

    _run(_coro())
    return None


def test_handle_set_baudrate_resp_calls_callback(
    runtime_config: RuntimeConfig,
    runtime_state: RuntimeState,
) -> None:
    """Test handle_set_baudrate_resp calls on_baudrate_change_ack if available."""
    async def _coro() -> None:
        ctx = DummyContext(runtime_config, runtime_state)
        component = SystemComponent(runtime_config, runtime_state, ctx)

        callback_called = False

        async def baudrate_ack_callback() -> None:
            nonlocal callback_called
            callback_called = True

        ctx.on_baudrate_change_ack = baudrate_ack_callback

        await component.handle_set_baudrate_resp(b"")

        assert callback_called

    _run(_coro())


def test_handle_set_baudrate_resp_no_callback(
    runtime_config: RuntimeConfig,
    runtime_state: RuntimeState,
) -> None:
    """Test handle_set_baudrate_resp handles missing callback gracefully."""
    async def _coro() -> None:
        ctx = DummyContext(runtime_config, runtime_state)
        component = SystemComponent(runtime_config, runtime_state, ctx)

        # No on_baudrate_change_ack attribute - should not raise
        await component.handle_set_baudrate_resp(b"")

    _run(_coro())


def test_handle_mqtt_unhandled_identifier(
    runtime_config: RuntimeConfig,
    runtime_state: RuntimeState,
) -> None:
    """Test handle_mqtt returns False for unknown identifiers."""
    async def _coro() -> None:
        ctx = DummyContext(runtime_config, runtime_state)
        component = SystemComponent(runtime_config, runtime_state, ctx)

        handled = await component.handle_mqtt(
            "unknown_action",
            ["get"],
            None,
        )

        assert handled is False
        assert not ctx.sent_frames
        assert not ctx.published

    _run(_coro())


def test_handle_mqtt_version_get_without_inbound(
    runtime_config: RuntimeConfig,
    runtime_state: RuntimeState,
) -> None:
    """Test handle_mqtt version/get without inbound message (no reply_context)."""
    async def _coro() -> None:
        ctx = DummyContext(runtime_config, runtime_state)
        component = SystemComponent(runtime_config, runtime_state, ctx)

        handled = await component.handle_mqtt(
            "version",
            ["get"],
            None,  # No inbound
        )

        assert handled is True
        assert ctx.sent_frames == [(Command.CMD_GET_VERSION.value, b"")]
        # No pending version since no inbound
        assert not component._pending_version

    _run(_coro())


def test_handle_mqtt_free_memory_get_without_inbound(
    runtime_config: RuntimeConfig,
    runtime_state: RuntimeState,
) -> None:
    """Test handle_mqtt free_memory/get without inbound message."""
    async def _coro() -> None:
        ctx = DummyContext(runtime_config, runtime_state)
        component = SystemComponent(runtime_config, runtime_state, ctx)

        handled = await component.handle_mqtt(
            "free_memory",
            ["get"],
            None,  # No inbound
        )

        assert handled is True
        assert ctx.sent_frames == [(Command.CMD_GET_FREE_MEMORY.value, b"")]
        # No pending since no inbound
        assert not component._pending_free_memory

    _run(_coro())
