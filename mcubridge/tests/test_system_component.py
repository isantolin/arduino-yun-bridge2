from typing import Any
import asyncio
from unittest.mock import AsyncMock, MagicMock

import pytest
from mcubridge.config.settings import RuntimeConfig
from mcubridge.protocol import protocol
from mcubridge.protocol.protocol import SystemAction
from mcubridge.protocol import structures
from mcubridge.services.base import BridgeContext
from mcubridge.services.system import SystemComponent
from mcubridge.state.context import RuntimeState, create_runtime_state


def _run(coro: Any):
    asyncio.run(coro)


@pytest.fixture
def runtime_config():
    return RuntimeConfig(
        serial_port="/dev/null",
        serial_baud=115200,
        serial_safe_baud=115200,
        mqtt_host="localhost",
        mqtt_port=1883,
        mqtt_topic="br",
        serial_shared_secret=b"secret12345",
    )


@pytest.fixture
def runtime_state(runtime_config: Any):
    state = create_runtime_state(runtime_config)
    try:
        yield state
    finally:
        state.cleanup()


class DummyContext(BridgeContext):
    def __init__(self: Any, config: Any, state: Any):
        super().__init__(config, state)  # type: ignore[reportCallIssue]
        self.sent_frames = []
        self.published = []
        self.background_tasks = []

    async def send_frame(self, command_id: int, payload: bytes) -> bool:  # type: ignore[reportIncompatibleMethodOverride]
        self.sent_frames.append((command_id, payload))  # type: ignore[reportUnknownMemberType]
        return True

    async def publish(self: Any, topic: str, payload: bytes, qos: int = 0, retain: bool = False, **kwargs: Any) -> None:  # type: ignore[reportIncompatibleMethodOverride]
        self.published.append((topic, payload, qos, retain))

    async def schedule_background(self: Any, coro: Any, name: Any=None) -> None:  # type: ignore[reportIncompatibleMethodOverride]
        self.background_tasks.append(coro)


def test_request_mcu_version_resets_cached_version(runtime_config: Any, runtime_state: Any):
    async def _coro():
        ctx = DummyContext(runtime_config, runtime_state)  # type: ignore[reportAbstractUsage]
        component = SystemComponent(runtime_config, runtime_state, ctx)
        runtime_state.mcu_version = (1, 0)

        await component.request_mcu_version()

        assert runtime_state.mcu_version is None
        assert len(ctx.sent_frames) == 1  # type: ignore[reportUnknownMemberType]
        cmd, pl = ctx.sent_frames[0]  # type: ignore[reportUnknownVariableType]
        assert cmd == protocol.Command.CMD_GET_VERSION.value
        assert pl == b""

    _run(_coro())


def test_handle_get_free_memory_resp_publishes_with_pending_reply(runtime_config: Any, runtime_state: Any):
    async def _coro():
        ctx = DummyContext(runtime_config, runtime_state)  # type: ignore[reportAbstractUsage]
        component = SystemComponent(runtime_config, runtime_state, ctx)

        msg = MagicMock()
        msg.topic = "reply/topic"
        component._pending_free_memory.append(msg)  # type: ignore[reportPrivateUsage]

        # Payload: 2 bytes (uint16)
        await component.handle_get_free_memory_resp(0, structures.FreeMemoryResponsePacket(value=1024).encode())

        # It publishes twice (one for reply, one for broadcast)
        assert len(ctx.published) == 2  # type: ignore[reportUnknownMemberType]
        # First is reply (usually)
        # Check that value 1024 is in payload
        assert "1024" in ctx.published[0][1]  # type: ignore[reportUnknownMemberType]
    _run(_coro())


def test_handle_get_free_memory_resp_ignores_malformed(runtime_config: Any, runtime_state: Any):
    async def _coro():
        ctx = DummyContext(runtime_config, runtime_state)  # type: ignore[reportAbstractUsage]
        component = SystemComponent(runtime_config, runtime_state, ctx)

        # Malformed payload (1 byte)
        await component.handle_get_free_memory_resp(0, b"\x00")

        assert len(ctx.published) == 0  # type: ignore[reportUnknownMemberType]

    _run(_coro())


def test_handle_get_version_resp_publishes_pending_and_updates_state(runtime_config: Any, runtime_state: Any):
    async def _coro():
        ctx = DummyContext(runtime_config, runtime_state)  # type: ignore[reportAbstractUsage]
        component = SystemComponent(runtime_config, runtime_state, ctx)

        msg = MagicMock()
        msg.topic = "reply/ver"
        component._pending_version.append(msg)  # type: ignore[reportPrivateUsage]

        # Payload: major=1, minor=2
        payload = structures.VersionResponsePacket(major=1, minor=2, patch=0).encode()

        await component.handle_get_version_resp(0, payload)

        assert runtime_state.mcu_version == (1, 2)
        assert len(ctx.published) >= 1  # type: ignore[reportUnknownMemberType]
        assert "1.2" in ctx.published[0][1]  # type: ignore[reportUnknownMemberType]
    _run(_coro())


def test_handle_get_version_resp_malformed(
    runtime_config: RuntimeConfig,
    runtime_state: RuntimeState,
    caplog: pytest.LogCaptureFixture,
) -> None:
    async def _coro() -> None:
        ctx = DummyContext(runtime_config, runtime_state)  # type: ignore[reportAbstractUsage]
        component = SystemComponent(runtime_config, runtime_state, ctx)

        caplog.set_level("WARNING", logger="mcubridge.system")

        # Use a payload that is definitely too short (1 byte instead of 2)
        await component.handle_get_version_resp(0, b"x")

        assert runtime_state.mcu_version is None
        assert not ctx.published  # type: ignore[reportUnknownMemberType]
        messages = (record.getMessage() for record in caplog.records)
        assert any(
            "Malformed structures.VersionResponsePacket" in msg
            or "Malformed VersionResponsePacket" in msg
            for msg in messages
        )

    _run(_coro())


def test_handle_mqtt_version_get_with_cached_version(runtime_config: Any, runtime_state: Any):
    async def _coro():
        ctx = DummyContext(runtime_config, runtime_state)  # type: ignore[reportAbstractUsage]
        component = SystemComponent(runtime_config, runtime_state, ctx)
        runtime_state.mcu_version = (2, 0)

        msg = MagicMock()
        msg.topic = "br/system/version/get"

        await component.handle_mqtt(SystemAction.VERSION, [SystemAction.GET], msg)

        assert len(ctx.published) >= 1  # type: ignore[reportUnknownMemberType]
        assert "2.0" in ctx.published[0][1]  # type: ignore[reportUnknownMemberType]
        # It still requests version to refresh cache
        assert len(ctx.sent_frames) == 1  # type: ignore[reportUnknownMemberType]
    _run(_coro())


def test_handle_mqtt_version_get_without_cached_version(runtime_config: Any, runtime_state: Any):
    async def _coro():
        ctx = DummyContext(runtime_config, runtime_state)  # type: ignore[reportAbstractUsage]
        component = SystemComponent(runtime_config, runtime_state, ctx)
        runtime_state.mcu_version = None

        msg = MagicMock()
        msg.topic = "br/system/version/get"

        await component.handle_mqtt(SystemAction.VERSION, [SystemAction.GET], msg)

        assert len(ctx.sent_frames) == 1  # type: ignore[reportUnknownMemberType]
        cmd, pl = ctx.sent_frames[0]  # type: ignore[reportUnknownVariableType]
        assert cmd == protocol.Command.CMD_GET_VERSION.value
        assert msg in component._pending_version  # type: ignore[reportPrivateUsage]

    _run(_coro())


def test_handle_mqtt_free_memory_get_tracks_pending(runtime_config: Any, runtime_state: Any):
    async def _coro():
        ctx = DummyContext(runtime_config, runtime_state)  # type: ignore[reportAbstractUsage]
        component = SystemComponent(runtime_config, runtime_state, ctx)

        msg = MagicMock()
        msg.topic = "br/system/memory/get"

        await component.handle_mqtt(SystemAction.FREE_MEMORY, [SystemAction.GET], msg)

        assert len(ctx.sent_frames) == 1  # type: ignore[reportUnknownMemberType]
        cmd, pl = ctx.sent_frames[0]  # type: ignore[reportUnknownVariableType]
        assert cmd == protocol.Command.CMD_GET_FREE_MEMORY.value
        assert msg in component._pending_free_memory  # type: ignore[reportPrivateUsage]

    _run(_coro())


def test_handle_set_baudrate_resp_calls_callback(runtime_config: Any, runtime_state: Any):
    async def _coro():
        ctx = DummyContext(runtime_config, runtime_state)  # type: ignore[reportAbstractUsage]
        component = SystemComponent(runtime_config, runtime_state, ctx)

        cb = AsyncMock()
        ctx.on_baudrate_change_ack = cb  # type: ignore[reportAttributeAccessIssue]

        await component.handle_set_baudrate_resp(0, b"")

        cb.assert_awaited_once()

    _run(_coro())
