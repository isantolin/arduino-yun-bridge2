"""Unit tests for the DatastoreComponent."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import msgspec
import pytest
from mcubridge.protocol.protocol import (
    DatastoreAction,
)
from mcubridge.protocol.structures import (
    DatastoreGetPacket,
    DatastorePutPacket,
)
from mcubridge.protocol.topics import Topic
from mcubridge.services.datastore import DatastoreComponent
from mcubridge.services.serial_flow import SerialFlowController
from mcubridge.state.context import RuntimeState
from mcubridge.transport.mqtt import MqttTransport
from tests._helpers import make_mqtt_msg, make_route, make_test_config


@pytest.fixture
def datastore_component() -> DatastoreComponent:
    config = make_test_config()
    state = MagicMock(spec=RuntimeState)
    state.datastore = {}
    state.mqtt_topic_prefix = "br"

    serial_flow = MagicMock(spec=SerialFlowController)
    serial_flow.acknowledge = AsyncMock()
    serial_flow.send = AsyncMock(return_value=True)
    mqtt_flow = MagicMock(spec=MqttTransport)
    mqtt_flow.publish = AsyncMock()

    return DatastoreComponent(
        config=config,
        state=state,
        serial_flow=serial_flow,
        mqtt_flow=mqtt_flow
    )


@pytest.mark.asyncio
async def test_datastore_handle_put(datastore_component: DatastoreComponent) -> None:
    payload = msgspec.msgpack.encode(DatastorePutPacket(key="temp", value=b"25.5"))
    await datastore_component.handle_put(0, payload)

    assert datastore_component.state.datastore["temp"] == "25.5"
    datastore_component.mqtt_flow.publish.assert_called()


@pytest.mark.asyncio
async def test_datastore_handle_get_request(datastore_component: DatastoreComponent) -> None:
    datastore_component.state.datastore["version"] = "1.0.0"
    payload = msgspec.msgpack.encode(DatastoreGetPacket(key="version"))

    await datastore_component.handle_get_request(0, payload)

    datastore_component.serial_flow.send.assert_called()
    datastore_component.mqtt_flow.publish.assert_called()


@pytest.mark.asyncio
async def test_datastore_handle_mqtt_put(datastore_component: DatastoreComponent) -> None:
    route = make_route(Topic.DATASTORE, DatastoreAction.PUT.value, "sys", "uptime")
    msg = make_mqtt_msg(b"3600")

    await datastore_component.handle_mqtt(route, msg)

    assert datastore_component.state.datastore["sys/uptime"] == "3600"
    datastore_component.mqtt_flow.publish.assert_called()


@pytest.mark.asyncio
async def test_datastore_handle_mqtt_get(datastore_component: DatastoreComponent) -> None:
    datastore_component.state.datastore["status"] = "OK"
    route = make_route(Topic.DATASTORE, DatastoreAction.GET.value, "status")
    msg = make_mqtt_msg(b"")

    await datastore_component.handle_mqtt(route, msg)

    datastore_component.mqtt_flow.publish.assert_called()
