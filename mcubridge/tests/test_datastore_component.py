"""Unit tests for DatastoreComponent behaviour (SIL-2)."""

from __future__ import annotations

import collections
from typing import Any, cast
from unittest.mock import AsyncMock, MagicMock

import msgspec
import pytest
from aiomqtt.message import Message

from mcubridge.config.settings import RuntimeConfig
from mcubridge.protocol import structures
from mcubridge.protocol.protocol import Command, DatastoreAction, Status, Topic
from mcubridge.protocol.structures import TopicRoute
from mcubridge.services.datastore import DatastoreComponent
from mcubridge.services.serial_flow import SerialFlowController
from mcubridge.state.context import create_runtime_state


@pytest.fixture
def datastore_component(
    runtime_config: RuntimeConfig,
) -> DatastoreComponent:
    state = create_runtime_state(runtime_config)
    state.mqtt_topic_prefix = "br"

    serial_flow = AsyncMock(spec=SerialFlowController)
    serial_flow.acknowledge = AsyncMock()
    serial_flow.send = AsyncMock(return_value=True)
    enqueue_mqtt = AsyncMock()

    return DatastoreComponent(
        config=runtime_config, state=state, serial_flow=serial_flow, enqueue_mqtt=enqueue_mqtt
    )


@pytest.mark.asyncio
async def test_datastore_handle_put(datastore_component: DatastoreComponent) -> None:
    # MCU puts a value
    pkt = structures.DatastorePutPacket(key="timeout", value=b"3600")
    payload = msgspec.msgpack.encode(pkt)

    await datastore_component.handle_put(0, payload)

    assert datastore_component.state.datastore["timeout"] == b"3600"
    cast(Any, datastore_component.enqueue_mqtt).assert_called()
    msg = cast(Any, datastore_component.enqueue_mqtt).call_args.args[0]
    assert msg.payload == b"3600"
    assert "timeout" in msg.topic_name


@pytest.mark.asyncio
async def test_datastore_handle_get_request(
    datastore_component: DatastoreComponent,
) -> None:
    # Setup data
    datastore_component.state.datastore["version"] = b"1.0"

    pkt = structures.DatastoreGetPacket(key="version")
    await datastore_component.handle_get_request(0, msgspec.msgpack.encode(pkt))

    cast(Any, datastore_component.serial_flow.send).assert_called()
    call_args = cast(Any, datastore_component.serial_flow.send).call_args
    assert call_args.args[0] == Command.CMD_DATASTORE_GET_RESP.value
    # Result payload should contain the value
    assert b"1.0" in call_args.args[1]


@pytest.mark.asyncio
async def test_datastore_handle_mqtt_put(
    datastore_component: DatastoreComponent,
) -> None:
    msg = MagicMock(spec=Message)
    msg.payload = b"3600"
    msg.topic = "br/ds/put/timeout"

    route = TopicRoute(
        raw="br/ds/put/timeout",
        prefix="br",
        topic=Topic.DATASTORE,
        segments=("put", "timeout"),
    )

    await datastore_component.handle_mqtt(route, msg)

    assert datastore_component.state.datastore["timeout"] == b"3600"
    cast(Any, datastore_component.serial_flow.send).assert_called()


@pytest.mark.asyncio
async def test_datastore_handle_mqtt_get(
    datastore_component: DatastoreComponent,
) -> None:
    datastore_component.state.datastore["timeout"] = b"3600"

    msg = MagicMock(spec=Message)
    msg.topic = "br/ds/get/timeout"
    msg.properties = None

    route = TopicRoute(
        raw="br/ds/get/timeout",
        prefix="br",
        topic=Topic.DATASTORE,
        segments=("get", "timeout"),
    )

    await datastore_component.handle_mqtt(route, msg)

    cast(Any, datastore_component.enqueue_mqtt).assert_called()
    msg_out = cast(Any, datastore_component.enqueue_mqtt).call_args.args[0]
    assert msg_out.payload == b"3600"
