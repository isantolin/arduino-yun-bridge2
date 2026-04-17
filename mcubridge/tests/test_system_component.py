from __future__ import annotations

import asyncio
from collections.abc import Coroutine
from typing import Any
from unittest.mock import MagicMock

import pytest
from aiomqtt import Message
from mcubridge.config.settings import RuntimeConfig
from mcubridge.protocol import protocol, structures
from mcubridge.protocol.protocol import SystemAction
from mcubridge.protocol.structures import QueuedPublish
from mcubridge.protocol.topics import Topic
from mcubridge.services.system import SystemComponent
from mcubridge.state.context import RuntimeState, create_runtime_state
from tests._helpers import make_route


def _run(coro: Coroutine[Any, Any, Any]) -> None:
    asyncio.run(coro)


@pytest.fixture
def runtime_config() -> RuntimeConfig:
    import tempfile
    return RuntimeConfig(
        serial_port="/dev/null",
        serial_baud=115200,
        serial_safe_baud=115200,
        mqtt_host="localhost",
        mqtt_port=1883,
        mqtt_topic="br",
        serial_shared_secret=b"secret12345",
        file_system_root=tempfile.mkdtemp(prefix="mcubridge-test-fs-"),
        mqtt_spool_dir=tempfile.mkdtemp(prefix="mcubridge-test-spool-"),
    )


@pytest.fixture
def runtime_state(runtime_config: RuntimeConfig):
    state = create_runtime_state(runtime_config)
    try:
        yield state
    finally:
        state.cleanup()


class DummyContext:
    """Minimal BridgeContext implementation for testing."""

    def __init__(self, config: RuntimeConfig, state: RuntimeState) -> None:
        self.config = config
        self.state = state
        self.sent_frames: list[tuple[int, bytes]] = []
        self.published: list[tuple[str, bytes | str, int, bool]] = []
        self.background_tasks: list[Coroutine[Any, Any, None]] = []

    async def send_frame(self, command_id: int, payload: bytes = b"") -> bool:
        self.sent_frames.append((command_id, payload))
        return True

    async def enqueue_mqtt(
        self,
        message: QueuedPublish,
        *,
        reply_context: Message | None = None,
    ) -> None:
        pass

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
        self.published.append((topic, payload, qos, retain))

    async def acknowledge_mcu_frame(
        self, command_id: int, seq_id: int, *, status: Any = None
    ) -> None:
        pass

    async def schedule_background(
        self,
        coroutine: Coroutine[Any, Any, None],
        *,
        name: str | None = None,
    ) -> asyncio.Task[Any]:
        self.background_tasks.append(coroutine)
        return asyncio.ensure_future(coroutine)


def test_request_mcu_version_resets_cached_version(
    runtime_config: RuntimeConfig, runtime_state: RuntimeState
):
    async def _coro():
        ctx = DummyContext(runtime_config, runtime_state)
        component = SystemComponent(runtime_config, runtime_state, ctx)
        runtime_state.mcu_version = (1, 0, 0)

        await component.request_mcu_version()

        assert runtime_state.mcu_version is None
        assert len(ctx.sent_frames) == 1
        cmd, pl = ctx.sent_frames[0]
        assert cmd == protocol.Command.CMD_GET_VERSION.value
        assert pl == b""

    _run(_coro())


def test_handle_get_free_memory_resp_publishes_with_pending_reply(
    runtime_config: RuntimeConfig, runtime_state: RuntimeState
):
    async def _coro():
        ctx = DummyContext(runtime_config, runtime_state)
        component = SystemComponent(runtime_config, runtime_state, ctx)

        msg = MagicMock()
        msg.topic = "reply/topic"
        getattr(component, "_pending_free_memory").append(msg)

        # Payload: 2 bytes (uint16)
        await component.handle_get_free_memory_resp(
            0, structures.FreeMemoryResponsePacket(value=1024).encode()
        )

        # It publishes twice (one for reply, one for broadcast)
        assert len(ctx.published) == 2
        # First is reply (usually)
        # Check that value 1024 is in payload
        assert "1024" in str(ctx.published[0][1])

    _run(_coro())


def test_handle_get_free_memory_resp_ignores_malformed(
    runtime_config: RuntimeConfig, runtime_state: RuntimeState
):
    async def _coro():
        ctx = DummyContext(runtime_config, runtime_state)
        component = SystemComponent(runtime_config, runtime_state, ctx)

        # Malformed payload (1 byte)
        await component.handle_get_free_memory_resp(0, b"\x00")

        assert len(ctx.published) == 0

    _run(_coro())


def test_handle_get_version_resp_publishes_pending_and_updates_state(
    runtime_config: RuntimeConfig, runtime_state: RuntimeState
):
    async def _coro():
        ctx = DummyContext(runtime_config, runtime_state)
        component = SystemComponent(runtime_config, runtime_state, ctx)

        msg = MagicMock()
        msg.topic = "reply/ver"
        getattr(component, "_pending_version").append(msg)

        # Payload: major=1, minor=2
        payload = structures.VersionResponsePacket(major=1, minor=2, patch=0).encode()

        await component.handle_get_version_resp(0, payload)

        assert runtime_state.mcu_version == (1, 2, 0)
        assert len(ctx.published) >= 1
        assert "1.2.0" in str(ctx.published[0][1])

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

        # Use a payload that is definitely too short (1 byte instead of 2)
        await component.handle_get_version_resp(0, b"x")

        assert runtime_state.mcu_version is None
        assert not ctx.published
        messages = (record.getMessage() for record in caplog.records)
        assert any(
            "Malformed structures.VersionResponsePacket" in msg
            or "Malformed VersionResponsePacket" in msg
            for msg in messages
        )

    _run(_coro())


def test_handle_mqtt_version_get_with_cached_version(
    runtime_config: RuntimeConfig, runtime_state: RuntimeState
):
    async def _coro():
        ctx = DummyContext(runtime_config, runtime_state)
        component = SystemComponent(runtime_config, runtime_state, ctx)
        runtime_state.mcu_version = (2, 0, 0)

        msg = MagicMock()
        msg.topic = "br/system/version/get"
        msg.payload = b""

        await component.handle_mqtt(
            make_route(Topic.SYSTEM, SystemAction.VERSION, SystemAction.GET), msg
        )

        assert len(ctx.published) >= 1
        assert "2.0.0" in str(ctx.published[0][1])
        # It still requests version to refresh cache
        assert len(ctx.sent_frames) == 1

    _run(_coro())


def test_handle_mqtt_version_get_without_cached_version(
    runtime_config: RuntimeConfig, runtime_state: RuntimeState
):
    async def _coro():
        ctx = DummyContext(runtime_config, runtime_state)
        component = SystemComponent(runtime_config, runtime_state, ctx)
        runtime_state.mcu_version = None

        msg = MagicMock()
        msg.topic = "br/system/version/get"
        msg.payload = b""

        await component.handle_mqtt(
            make_route(Topic.SYSTEM, SystemAction.VERSION, SystemAction.GET), msg
        )

        assert len(ctx.sent_frames) == 1
        cmd, _pl = ctx.sent_frames[0]
        assert cmd == protocol.Command.CMD_GET_VERSION.value
        assert msg in getattr(component, "_pending_version")

    _run(_coro())


def test_handle_mqtt_free_memory_get_tracks_pending(
    runtime_config: RuntimeConfig, runtime_state: RuntimeState
):
    async def _coro():
        ctx = DummyContext(runtime_config, runtime_state)
        component = SystemComponent(runtime_config, runtime_state, ctx)

        msg = MagicMock()
        msg.topic = "br/system/memory/get"
        msg.payload = b""

        await component.handle_mqtt(
            make_route(Topic.SYSTEM, SystemAction.FREE_MEMORY, SystemAction.GET), msg
        )

        assert len(ctx.sent_frames) == 1
        cmd, _pl = ctx.sent_frames[0]
        assert cmd == protocol.Command.CMD_GET_FREE_MEMORY.value
        assert msg in getattr(component, "_pending_free_memory")

    _run(_coro())
