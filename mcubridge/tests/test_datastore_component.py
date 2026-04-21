"""Unit tests for mcubridge.services.datastore (SIL-2)."""

from __future__ import annotations
import msgspec

from unittest.mock import AsyncMock, MagicMock

import pytest
from mcubridge.config.settings import RuntimeConfig
from mcubridge.protocol import structures
from mcubridge.protocol.protocol import DatastoreAction
from mcubridge.protocol.topics import Topic
from mcubridge.services.datastore import DatastoreComponent
from mcubridge.state.context import RuntimeState, create_runtime_state
from tests._helpers import make_mqtt_msg, make_route


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


@pytest.mark.asyncio
async def test_handle_put_success(
    serial_flow: MagicMock,
    mqtt_flow: MagicMock,
    runtime_config: RuntimeConfig,
    runtime_state: RuntimeState,
) -> None:
    component = DatastoreComponent(runtime_config, runtime_state, serial_flow, mqtt_flow)
    key = "testkey"
    value = b"testvalue"
    payload = msgspec.msgpack.encode(structures.DatastorePutPacket(key=key, value=value))

    await component.handle_put(0, payload)

    assert runtime_state.datastore.get(key) == "testvalue"
    mqtt_flow.publish.assert_called()


@pytest.mark.asyncio
async def test_handle_get_request_success(
    serial_flow: MagicMock,
    mqtt_flow: MagicMock,
    runtime_config: RuntimeConfig,
    runtime_state: RuntimeState,
) -> None:
    component = DatastoreComponent(runtime_config, runtime_state, serial_flow, mqtt_flow)
    runtime_state.datastore["mykey"] = "myvalue"
    payload = msgspec.msgpack.encode(structures.DatastoreGetPacket(key="mykey"))

    await component.handle_get_request(0, payload)

    serial_flow.send.assert_called_once()
    # Verify it published the result to MQTT as well
    mqtt_flow.publish.assert_called()


@pytest.mark.asyncio
async def test_handle_mqtt_put(
    serial_flow: MagicMock,
    mqtt_flow: MagicMock,
    runtime_config: RuntimeConfig,
    runtime_state: RuntimeState,
) -> None:
    component = DatastoreComponent(runtime_config, runtime_state, serial_flow, mqtt_flow)

    await component.handle_mqtt(
        make_route(Topic.DATASTORE, DatastoreAction.PUT.value, "newkey"),
        make_mqtt_msg(b"newval"),
    )

    assert runtime_state.datastore.get("newkey") == "newval"
    mqtt_flow.publish.assert_called()


@pytest.mark.asyncio
async def test_handle_mqtt_get_request(
    serial_flow: MagicMock,
    mqtt_flow: MagicMock,
    runtime_config: RuntimeConfig,
    runtime_state: RuntimeState,
) -> None:
    component = DatastoreComponent(runtime_config, runtime_state, serial_flow, mqtt_flow)
    runtime_state.datastore["reqkey"] = "reqval"

    # Simulate a get request via MQTT
    await component.handle_mqtt(
        make_route(Topic.DATASTORE, DatastoreAction.GET.value, "reqkey", "request"),
        make_mqtt_msg(b""),
    )

    mqtt_flow.publish.assert_called()
    args, kwargs = mqtt_flow.publish.call_args
    pld = kwargs.get("payload") or args[1]
    assert pld == b"reqval"
