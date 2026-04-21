"""Unit tests for mcubridge.services.system (SIL-2)."""

from __future__ import annotations

from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest
from mcubridge.config.settings import RuntimeConfig
from mcubridge.protocol import structures
from mcubridge.protocol.protocol import SystemAction
from mcubridge.protocol.topics import Topic
from mcubridge.services.system import SystemComponent
from mcubridge.state.context import RuntimeState, create_runtime_state
from tests._helpers import make_route


@pytest.fixture
def runtime_config() -> RuntimeConfig:
    import tempfile

    return RuntimeConfig(
        serial_port="/dev/null",
        mqtt_topic="br",
        file_system_root=tempfile.mkdtemp(prefix="mcubridge-test-fs-"),
        mqtt_spool_dir=tempfile.mkdtemp(prefix="mcubridge-test-spool-"),
        serial_shared_secret=b"s_e_c_r_e_t_mock",
    )


@pytest.fixture
def runtime_state(runtime_config: RuntimeConfig) -> RuntimeState:
    state = create_runtime_state(runtime_config)
    return state


@pytest.fixture
def serial_flow() -> MagicMock:
    sf = MagicMock()
    sf.send = AsyncMock(return_value=True)
    sf.acknowledge = AsyncMock()
    return sf


@pytest.fixture
def mqtt_flow() -> MagicMock:
    mf = MagicMock()
    mf.publish = AsyncMock()
    mf.enqueue_mqtt = AsyncMock()
    return mf


def _get_publish_arg(mock_pub: Any, arg_idx: int, kw_name: str, call_idx: int = -1) -> Any:
    """Robustly extract argument from mock call on mock_pub."""
    if not mock_pub.called:
        return None
    call = mock_pub.call_args_list[call_idx]
    if len(call.args) > arg_idx:
        return call.args[arg_idx]
    return call.kwargs.get(kw_name)


@pytest.mark.asyncio
async def test_handle_get_free_memory_resp_publishes_with_pending_reply(
    runtime_config: RuntimeConfig, runtime_state: RuntimeState, serial_flow: MagicMock, mqtt_flow: MagicMock
) -> None:
    component = SystemComponent(runtime_config, runtime_state, serial_flow, mqtt_flow)

    # 1. Simulate MQTT request
    inbound = type("MockMsg", (), {"topic": "br/system/free_memory/get", "payload": b""})()
    await component.handle_mqtt(
        make_route(Topic.SYSTEM, SystemAction.FREE_MEMORY.value, SystemAction.GET.value),
        inbound,  # type: ignore
    )

    serial_flow.send.assert_called_once()

    # 2. Simulate MCU response
    payload = structures.FreeMemoryResponsePacket(value=1024).encode()
    await component.handle_get_free_memory_resp(0, payload)

    assert mqtt_flow.publish.called
    # SystemComponent publishes twice: once for broadcast, once for reply context
    pub = mqtt_flow.publish
    topics = [str(_get_publish_arg(pub, 0, "topic", i)) for i in range(len(pub.call_args_list))]
    assert any("free_memory/value" in t for t in topics)


@pytest.mark.asyncio
async def test_handle_get_free_memory_resp_ignores_malformed(
    runtime_config: RuntimeConfig, runtime_state: RuntimeState, serial_flow: MagicMock, mqtt_flow: MagicMock
):
    component = SystemComponent(runtime_config, runtime_state, serial_flow, mqtt_flow)
    await component.handle_get_free_memory_resp(0, b"\xff")
    assert mqtt_flow.publish.call_count == 0


@pytest.mark.asyncio
async def test_handle_mqtt_free_memory_get_tracks_pending(
    runtime_config: RuntimeConfig, runtime_state: RuntimeState, serial_flow: MagicMock, mqtt_flow: MagicMock
):
    component = SystemComponent(runtime_config, runtime_state, serial_flow, mqtt_flow)

    # Fill internal pending queue
    for _ in range(10):
        await component.handle_mqtt(
            make_route(Topic.SYSTEM, SystemAction.FREE_MEMORY.value, SystemAction.GET.value),
            type("MockMsg", (), {"topic": "br/system/free_memory/get", "payload": b""})(),  # type: ignore
        )

    serial_flow.send.reset_mock()
    # This one should be rejected due to queue full
    await component.handle_mqtt(
        make_route(Topic.SYSTEM, SystemAction.FREE_MEMORY.value, SystemAction.GET.value),
        type("MockMsg", (), {"topic": "br/system/free_memory/get", "payload": b""})(),  # type: ignore
    )
    serial_flow.send.assert_not_called()


@pytest.mark.asyncio
async def test_handle_mqtt_version_get_without_cached_version(
    runtime_config: RuntimeConfig, runtime_state: RuntimeState, serial_flow: MagicMock, mqtt_flow: MagicMock
):
    component = SystemComponent(runtime_config, runtime_state, serial_flow, mqtt_flow)

    await component.handle_mqtt(
        make_route(Topic.SYSTEM, SystemAction.VERSION.value, SystemAction.GET.value),
        type("MockMsg", (), {"topic": "br/system/version/get", "payload": b""})(),  # type: ignore
    )

    serial_flow.send.assert_called_once()


@pytest.mark.asyncio
async def test_handle_mqtt_version_get_with_cached_version(
    runtime_config: RuntimeConfig, runtime_state: RuntimeState, serial_flow: MagicMock, mqtt_flow: MagicMock
):
    runtime_state.mcu_version = (1, 2, 0)
    component = SystemComponent(runtime_config, runtime_state, serial_flow, mqtt_flow)

    await component.handle_mqtt(
        make_route(Topic.SYSTEM, SystemAction.VERSION.value, SystemAction.GET.value),
        type("MockMsg", (), {"topic": "br/system/version/get", "payload": b""})(),  # type: ignore
    )

    # SystemComponent ALWAYS requests fresh version to sync cache,
    # even if it has a cached one.
    assert serial_flow.send.called
    assert mqtt_flow.publish.called
    pub = mqtt_flow.publish
    payloads = [str(_get_publish_arg(pub, 1, "payload", i)) for i in range(len(pub.call_args_list))]
    assert any("1.2.0" in p for p in payloads)


@pytest.mark.asyncio
async def test_handle_get_version_resp_publishes_pending_and_updates_state(
    runtime_config: RuntimeConfig, runtime_state: RuntimeState, serial_flow: MagicMock, mqtt_flow: MagicMock
):
    component = SystemComponent(runtime_config, runtime_state, serial_flow, mqtt_flow)

    # 1. Request version
    inbound = type("MockMsg", (), {"topic": "br/system/version/get", "payload": b""})()
    await component.handle_mqtt(
        make_route(Topic.SYSTEM, SystemAction.VERSION.value, SystemAction.GET.value),
        inbound,  # type: ignore
    )

    # 2. Receive response
    await component.handle_get_version_resp(0, structures.VersionResponsePacket(major=2, minor=0, patch=0).encode())

    assert runtime_state.mcu_version == (2, 0, 0)
    assert mqtt_flow.publish.called
    pub = mqtt_flow.publish
    payloads = [str(_get_publish_arg(pub, 1, "payload", i)) for i in range(len(pub.call_args_list))]
    assert any("2.0.0" in p for p in payloads)


@pytest.mark.asyncio
async def test_handle_get_version_resp_malformed(
    runtime_config: RuntimeConfig, runtime_state: RuntimeState, serial_flow: MagicMock, mqtt_flow: MagicMock
):
    component = SystemComponent(runtime_config, runtime_state, serial_flow, mqtt_flow)
    await component.handle_get_version_resp(0, b"\x00")
    assert runtime_state.mcu_version is None


@pytest.mark.asyncio
async def test_request_mcu_version_resets_cached_version(
    runtime_config: RuntimeConfig, runtime_state: RuntimeState, serial_flow: MagicMock, mqtt_flow: MagicMock
):
    runtime_state.mcu_version = (1, 1, 1)
    component = SystemComponent(runtime_config, runtime_state, serial_flow, mqtt_flow)

    await component.request_mcu_version()
    assert runtime_state.mcu_version is None
    serial_flow.send.assert_called_once()
