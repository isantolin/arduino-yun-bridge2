"""
Coverage gap filler tests for Python.
"""

from __future__ import annotations
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch
import pytest
from mcubridge.config.settings import RuntimeConfig
from mcubridge.protocol.protocol import Topic
from mcubridge.protocol.topics import TopicRoute
from mcubridge.services.runtime import BridgeService
from mcubridge.state.context import create_runtime_state
from aiomqtt.message import Message


@pytest.fixture
def runtime_config() -> RuntimeConfig:
    from mcubridge.config.const import (
        DEFAULT_MQTT_PORT,
        DEFAULT_PROCESS_TIMEOUT,
        DEFAULT_RECONNECT_DELAY,
        DEFAULT_STATUS_INTERVAL,
    )

    import tempfile
    return RuntimeConfig(
        serial_port="/dev/null",
        serial_baud=115200,
        serial_safe_baud=115200,
        mqtt_host="localhost",
        mqtt_port=DEFAULT_MQTT_PORT,
        mqtt_topic="bridge",
        allowed_commands=("true",),
        file_system_root=tempfile.mkdtemp(prefix="mcubridge-test-fs-"),
        mqtt_spool_dir=tempfile.mkdtemp(prefix="mcubridge-test-spool-"),
        process_timeout=DEFAULT_PROCESS_TIMEOUT,
        reconnect_delay=DEFAULT_RECONNECT_DELAY,
        status_interval=DEFAULT_STATUS_INTERVAL,
        debug_logging=False,
        process_max_concurrent=2,
        serial_shared_secret=b"secret",
    )


@pytest.fixture
def runtime_state(runtime_config: RuntimeConfig):
    state = create_runtime_state(runtime_config)
    try:
        yield state
    finally:
        state.cleanup()


@pytest.fixture
def service(runtime_config: RuntimeConfig, runtime_state: Any):
    s = BridgeService(runtime_config, runtime_state)
    # Mock serial sender to avoid real I/O
    s.register_serial_sender(AsyncMock())
    return s


# --- Dispatcher Gaps (Now in BridgeService) ---


@pytest.mark.asyncio
async def test_service_mcu_handler_exception(service: BridgeService):
    """Cover Exception in MCU handler logic in BridgeService."""

    async def buggy_handler(seq_id: Any, payload: Any):
        raise RuntimeError("bug")

    service._mcu_registry[0x99] = buggy_handler  # type: ignore[reportPrivateUsage]

    with patch.object(service.state, "is_synchronized", True):
        await service.handle_mcu_frame(0x99, 0, b"")
        # Should call send_frame with Status.ERROR
        cast_sender = service._serial_sender  # type: ignore[reportPrivateUsage]
        cast_sender.assert_called()  # type: ignore[reportAttributeAccessIssue]


@pytest.mark.asyncio
async def test_service_mqtt_no_segments(service: BridgeService):
    """Cover early exit in handle_mqtt_message if no segments."""
    msg = MagicMock(spec=Message)
    msg.topic = "bridge/system"
    msg.payload = b""

    with patch("mcubridge.services.runtime.parse_topic") as mock_parse:
        mock_parse.return_value = TopicRoute(raw="bridge/system", prefix="bridge", topic=Topic.SYSTEM, segments=())
        await service.handle_mqtt_message(msg)


@pytest.mark.asyncio
async def test_service_mqtt_handler_exception(service: BridgeService):
    """Cover Exception in MQTT handler logic in BridgeService."""
    msg = MagicMock(spec=Message)
    msg.topic = "bridge/system/test"
    msg.payload = b""

    route = TopicRoute(
        raw=str(msg.topic), prefix="bridge", topic=Topic.SYSTEM, segments=("test",)
    )

    with patch.object(
        service._mqtt_router, "dispatch", side_effect=RuntimeError("mqtt bug")  # type: ignore[reportPrivateUsage]
    ):
        with patch("mcubridge.services.runtime.parse_topic", return_value=route):
            await service.handle_mqtt_message(msg)


@pytest.mark.asyncio
async def test_service_should_reject_topic_action_gaps(service: BridgeService):
    """Cover policy rejection in handle_mqtt_message."""
    msg = MagicMock(spec=Message)
    msg.topic = "br/digital/13"
    msg.payload = b"1"
    msg.properties = None

    route = TopicRoute(
        raw="br/digital/13", prefix="br", topic=Topic.DIGITAL, segments=("13",)
    )

    with patch.object(service, "_is_topic_action_allowed", return_value=False):
        with patch.object(service, "_reject_topic_action", new_callable=AsyncMock) as mock_rej:
            with patch("mcubridge.services.runtime.parse_topic", return_value=route):
                await service.handle_mqtt_message(msg)
                mock_rej.assert_awaited_once()


@pytest.mark.asyncio
async def test_service_handle_bridge_topic_no_segments(service: BridgeService):
    """Cover unhandled bridge remainder segments."""
    route = TopicRoute(
        raw="br/system/bridge/unknown",
        prefix="br",
        topic=Topic.SYSTEM,
        segments=("bridge", "unknown"),
    )
    msg = MagicMock(spec=Message)
    # type: ignore[reportPrivateUsage]
    result = await service._handle_mqtt_system(route, msg) # type: ignore[reportPrivateUsage]
    assert result is False

@pytest.mark.asyncio
async def test_datastore_publish_value_error_reason(
    runtime_config: Any, runtime_state: Any
):
    """Cover logic in _publish_datastore_value."""
    ctx = MagicMock()
    ctx.publish = AsyncMock()
    from mcubridge.services.datastore import DatastoreComponent
    ds = DatastoreComponent(runtime_config, runtime_state, ctx)
    await ds._publish_datastore_value(  # type: ignore[reportPrivateUsage]
        key="key",
        value=b"val",
        error_reason="testing",
    )
    _args, kwargs = ctx.publish.call_args
    props = kwargs.get("properties", ())
    assert any(k == "bridge-error" and v == "testing" for k, v in props)
