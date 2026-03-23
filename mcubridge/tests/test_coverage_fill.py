"""
Coverage gap filler tests for Python.
"""
from __future__ import annotations
from unittest.mock import AsyncMock, MagicMock, patch
import pytest
from mcubridge.config.settings import RuntimeConfig
from mcubridge.protocol.protocol import Command, Topic
from mcubridge.services.dispatcher import BridgeDispatcher
from mcubridge.protocol.topics import TopicRoute
from mcubridge.services.datastore import DatastoreComponent
from mcubridge.state.context import create_runtime_state
from aiomqtt.message import Message

from .conftest import make_component_container

@pytest.fixture
def runtime_config() -> RuntimeConfig:
    from mcubridge.config.const import (
        DEFAULT_MQTT_PORT, DEFAULT_PROCESS_TIMEOUT, DEFAULT_RECONNECT_DELAY, DEFAULT_STATUS_INTERVAL
    )
    return RuntimeConfig(
        serial_port="/dev/null",
        serial_baud=115200,
        serial_safe_baud=115200,
        mqtt_host="localhost",
        mqtt_port=DEFAULT_MQTT_PORT,
        mqtt_topic="bridge",
        allowed_commands=("true",),
        file_system_root="/tmp",
        process_timeout=DEFAULT_PROCESS_TIMEOUT,
        reconnect_delay=DEFAULT_RECONNECT_DELAY,
        status_interval=DEFAULT_STATUS_INTERVAL,
        debug_logging=False,
        process_max_concurrent=2,
        serial_shared_secret=b"secret",
    )

@pytest.fixture
def runtime_state(runtime_config: RuntimeConfig):
    return create_runtime_state(runtime_config)

@pytest.fixture
def dispatcher(runtime_config: RuntimeConfig, runtime_state):
    mcu_registry = MagicMock()
    mqtt_router = MagicMock()
    d = BridgeDispatcher(
        mcu_registry=mcu_registry,
        mqtt_router=mqtt_router,
        state=runtime_state,
        send_frame=AsyncMock(),
        acknowledge_frame=AsyncMock(),
        is_topic_action_allowed=lambda t, a: True,
        reject_topic_action=AsyncMock(),
        publish_bridge_snapshot=AsyncMock(),
    )
    # Register components with mocks
    d.register_components(
        make_component_container(
            console=MagicMock(),
            datastore=MagicMock(),
            file=MagicMock(),
            mailbox=MagicMock(),
            pin=MagicMock(),
            process=MagicMock(),
            spi=MagicMock(),
            system=MagicMock(),
        )
    )
    return d

# --- Dispatcher Gaps ---

@pytest.mark.asyncio
async def test_dispatcher_pin_not_registered(dispatcher: BridgeDispatcher):
    """Cover line 165-166 in dispatcher.py (Pin component not registered)."""
    dispatcher._container = None
    # CMD_DIGITAL_READ = 0x23
    # Find the handler registered for CMD_DIGITAL_READ
    handler = None
    for call in dispatcher.mcu_registry.register.call_args_list:
        if call[0][0] == Command.CMD_DIGITAL_READ.value:
            handler = call[0][1]
            break

    assert handler is not None
    result = await handler(0, b"\x01")
    assert result is False

@pytest.mark.asyncio
async def test_dispatcher_mcu_handler_exception(dispatcher: BridgeDispatcher):
    """Cover lines 258-272 in dispatcher.py (Exception in MCU handler)."""
    async def buggy_handler(seq_id, payload):
        raise RuntimeError("bug")

    dispatcher.mcu_registry.get.return_value = buggy_handler
    # Use patch to set is_synchronized
    with patch.object(type(dispatcher.state), "is_synchronized", True):
        await dispatcher.dispatch_mcu_frame(0x99, 0, b"")
        dispatcher.send_frame.assert_called()

@pytest.mark.asyncio
async def test_dispatcher_mqtt_no_segments(dispatcher: BridgeDispatcher):
    """Cover line 283-284 in dispatcher.py."""
    msg = MagicMock(spec=Message)
    msg.topic = "bridge/system"
    msg.payload = b""

    def parse_mock(t):
        return TopicRoute(raw=t, prefix="bridge", topic=Topic.SYSTEM, segments=())

    await dispatcher.dispatch_mqtt_message(msg, parse_mock)

@pytest.mark.asyncio
async def test_dispatcher_mqtt_handler_exception(dispatcher: BridgeDispatcher):
    """Cover lines 288-292 in dispatcher.py."""
    msg = MagicMock(spec=Message)
    msg.topic = "bridge/system/test"
    msg.payload = b""

    route = TopicRoute(raw=str(msg.topic), prefix="bridge", topic=Topic.SYSTEM, segments=("test",))

    with patch.object(dispatcher.mqtt_router, "dispatch", side_effect=RuntimeError("mqtt bug")):
        await dispatcher.dispatch_mqtt_message(msg, lambda t: route)

@pytest.mark.asyncio
async def test_dispatcher_should_reject_topic_action_gaps(dispatcher: BridgeDispatcher):
    """Cover lines 316, 319 in dispatcher.py."""
    # Line 316: Topic.DIGITAL with no segments
    route1 = TopicRoute(raw="", prefix="bridge", topic=Topic.DIGITAL, segments=())
    assert dispatcher._should_reject_topic_action(route1) is None

    # Line 319: len(segments) > 1 but segments[1] is empty
    route2 = TopicRoute(raw="", prefix="bridge", topic=Topic.DIGITAL, segments=("1", ""))
    assert dispatcher._should_reject_topic_action(route2) is None

@pytest.mark.asyncio
async def test_dispatcher_handle_system_topic_no_component(dispatcher: BridgeDispatcher):
    """Cover line 347 in dispatcher.py."""
    dispatcher._container = None
    route = TopicRoute(raw="", prefix="bridge", topic=Topic.SYSTEM, segments=("unknown",))
    result = await dispatcher._handle_system_topic(route, MagicMock())
    assert result is False

@pytest.mark.asyncio
async def test_dispatcher_handle_bridge_topic_no_segments(dispatcher: BridgeDispatcher):
    """Cover lines 360-361 in dispatcher.py."""
    route = TopicRoute(raw="", prefix="bridge", topic=Topic.SYSTEM, segments=("bridge",))
    result = await dispatcher._handle_bridge_topic(route, MagicMock())
    assert result is False

# --- Datastore Gaps ---

@pytest.mark.asyncio
async def test_datastore_publish_value_error_reason(runtime_config, runtime_state):
    """Cover line 174 in datastore.py."""
    ctx = MagicMock()
    ctx.publish = AsyncMock()
    ds = DatastoreComponent(runtime_config, runtime_state, ctx)
    await ds._publish_value(topic="key", payload=b"val", expiry=60, properties=(("bridge-error", "testing"),))
    args, kwargs = ctx.publish.call_args
    props = kwargs.get("properties", ())
    assert any(k == "bridge-error" and v == "testing" for k, v in props)

@pytest.mark.asyncio
async def test_datastore_handle_get_request_fail_send(runtime_config, runtime_state):
    """Cover line 86->88 in datastore.py."""
    ctx = MagicMock()
    ctx.send_frame = AsyncMock(return_value=False)
    ds = DatastoreComponent(runtime_config, runtime_state, ctx)
    from mcubridge.protocol.structures import DatastoreGetPacket
    payload = DatastoreGetPacket(key="test").encode()
    result = await ds.handle_get_request(0, payload)
    assert result is False
