# pyright: reportPrivateUsage=false
"""
Coverage gap filler tests for Python.
"""

from __future__ import annotations
from mcubridge.services.pin import PinComponent
from mcubridge.transport.mqtt import MqttTransport
from mcubridge.router.routers import MQTTRouter
from mcubridge.services.process import ProcessComponent
from mcubridge.services.system import SystemComponent
from mcubridge.services.file import FileComponent
from mcubridge.services.serial_flow import SerialFlowController
from mcubridge.services.console import ConsoleComponent
from mcubridge.services.spi import SpiComponent
from mcubridge.services.mailbox import MailboxComponent
import msgspec
from typing import Any
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
        file_system_root=tempfile.mkdtemp(
            prefix="mcubridge-test-fs-", dir=".tmp_tests"
        ),
        mqtt_spool_dir=tempfile.mkdtemp(
            prefix="mcubridge-test-spool-", dir=".tmp_tests"
        ),
        process_timeout=DEFAULT_PROCESS_TIMEOUT,
        reconnect_delay=DEFAULT_RECONNECT_DELAY,
        status_interval=DEFAULT_STATUS_INTERVAL,
        debug=False,
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
def dispatcher(runtime_config: RuntimeConfig, runtime_state: Any):
    mcu_registry: dict[int, Any] = {}
    mqtt_router = AsyncMock(spec=MQTTRouter)
    mqtt_router.dispatch = AsyncMock(return_value=True)
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
    console = AsyncMock(spec=ConsoleComponent)
    datastore = AsyncMock(spec=DatastoreComponent)
    file = AsyncMock(spec=FileComponent)
    mailbox = AsyncMock(spec=MailboxComponent)
    pin = AsyncMock(spec=PinComponent)
    pin.handle_mcu_digital_read = AsyncMock(return_value=False)
    pin.handle_mcu_analog_read = AsyncMock(return_value=False)
    process = AsyncMock(spec=ProcessComponent)
    spi = AsyncMock(spec=SpiComponent)
    system = AsyncMock(spec=SystemComponent)

    d.register_components(
        make_component_container(
            console=console,
            datastore=datastore,
            file=file,
            mailbox=mailbox,
            pin=pin,
            process=process,
            spi=spi,
            system=system,
        )
    )
    return d


# --- Dispatcher Gaps ---


@pytest.mark.asyncio
async def test_dispatcher_pin_not_registered(dispatcher: BridgeDispatcher):
    """Cover line 165-166 in dispatcher.py (Pin component not registered)."""
    dispatcher._container = None  # type: ignore[reportPrivateUsage]
    # CMD_DIGITAL_READ = 0x23
    # Find the handler registered for CMD_DIGITAL_READ
    handler = dispatcher.mcu_registry.get(Command.CMD_DIGITAL_READ.value)

    assert handler is not None
    result = await handler(0, b"\x01")  # type: ignore[reportUnknownVariableType]
    assert result is False


@pytest.mark.asyncio
async def test_dispatcher_mcu_handler_exception(dispatcher: BridgeDispatcher):
    """Confirm that MCU handler exceptions bubble up directly."""

    async def buggy_handler(seq_id: Any, payload: Any):
        raise RuntimeError("bug")

    dispatcher.mcu_registry[0x99] = buggy_handler
    # Use patch to set is_synchronized
    with patch.object(type(dispatcher.state), "is_synchronized", True):
        with pytest.raises(RuntimeError, match="bug"):
            await dispatcher.dispatch_mcu_frame(0x99, 0, b"")


@pytest.mark.asyncio
async def test_dispatcher_mqtt_no_segments(dispatcher: BridgeDispatcher):
    """Cover line 283-284 in dispatcher.py."""
    msg = MagicMock(spec=Message)
    msg.topic = "bridge/system"
    msg.payload = b""

    def parse_mock(t: Any):
        return TopicRoute(raw=t, prefix="bridge", topic=Topic.SYSTEM, segments=())

    await dispatcher.dispatch_mqtt_message(msg, parse_mock)


@pytest.mark.asyncio
async def test_dispatcher_mqtt_handler_exception(dispatcher: BridgeDispatcher):
    """Confirm that MQTT handler exceptions bubble up directly."""
    msg = MagicMock(spec=Message)
    msg.topic = "bridge/system/test"
    msg.payload = b""

    route = TopicRoute(
        raw=str(msg.topic), prefix="bridge", topic=Topic.SYSTEM, segments=("test",)
    )

    with patch.object(
        dispatcher.mqtt_router, "dispatch", side_effect=RuntimeError("mqtt bug")
    ):
        with pytest.raises(RuntimeError, match="mqtt bug"):
            await dispatcher.dispatch_mqtt_message(msg, lambda t: route)


@pytest.mark.asyncio
async def test_dispatcher_should_reject_topic_action_gaps(dispatcher: BridgeDispatcher):
    """Cover lines 316, 319 in dispatcher.py."""
    # Line 316: Topic.DIGITAL with no segments
    route1 = TopicRoute(raw="", prefix="bridge", topic=Topic.DIGITAL, segments=())
    assert dispatcher._get_topic_action(route1) is None  # type: ignore[reportPrivateUsage]

    # Line 319: len(segments) > 1 but segments[1] is empty
    route2 = TopicRoute(
        raw="", prefix="bridge", topic=Topic.DIGITAL, segments=("1", "")
    )
    assert dispatcher._get_topic_action(route2) is None  # type: ignore[reportPrivateUsage]


@pytest.mark.asyncio
async def test_dispatcher_handle_system_topic_no_component(
    dispatcher: BridgeDispatcher,
):
    """Cover line 347 in dispatcher.py."""
    dispatcher._container = None  # type: ignore[reportPrivateUsage]
    route = TopicRoute(
        raw="", prefix="bridge", topic=Topic.SYSTEM, segments=("unknown",)
    )
    result = await dispatcher._handle_system_topic(route, MagicMock())  # type: ignore[reportPrivateUsage]
    assert result is False


@pytest.mark.asyncio
async def test_dispatcher_handle_bridge_topic_no_segments(dispatcher: BridgeDispatcher):
    """Cover lines 360-361 in dispatcher.py."""
    route = TopicRoute(
        raw="", prefix="bridge", topic=Topic.SYSTEM, segments=("bridge",)
    )
    result = await dispatcher._handle_bridge_topic(route, MagicMock())  # type: ignore[reportPrivateUsage]
    assert result is False


# --- Datastore Gaps ---


@pytest.mark.asyncio
async def test_datastore_publish_value_error_reason(
    runtime_config: Any, runtime_state: Any
):
    """Cover logic in _publish_datastore_value."""
    serial_flow = AsyncMock(spec=SerialFlowController)
    mqtt_flow = AsyncMock(spec=MqttTransport)
    mqtt_flow.enqueue_mqtt = AsyncMock()

    ds = DatastoreComponent(
        config=runtime_config,
        state=runtime_state,
        serial_flow=serial_flow,
        mqtt_flow=mqtt_flow,
    )
    await ds._publish_datastore_value(  # type: ignore[reportPrivateUsage]
        key="key",
        value=b"val",
        error_reason="testing",
    )
    # Check mqtt_flow.enqueue_mqtt instead of state.publish
    _args, _kwargs = mqtt_flow.enqueue_mqtt.call_args
    msg = _args[0]
    props = msg.user_properties
    assert any(k == "bridge-error" and v == "testing" for k, v in props)


@pytest.mark.asyncio
async def test_datastore_handle_get_request_fail_send(
    runtime_config: Any, runtime_state: Any
):
    """Cover line 86->88 in datastore.py."""
    serial_flow = AsyncMock(spec=SerialFlowController)
    serial_flow.send = AsyncMock(return_value=False)
    mqtt_flow = AsyncMock(spec=MqttTransport)

    ds = DatastoreComponent(
        config=runtime_config,
        state=runtime_state,
        serial_flow=serial_flow,
        mqtt_flow=mqtt_flow,
    )
    from mcubridge.protocol.structures import DatastoreGetPacket

    payload = msgspec.msgpack.encode(DatastoreGetPacket(key="test"))
    result = await ds.handle_get_request(0, payload)
    assert result is False
