"""Tests for the DatastoreComponent."""

from __future__ import annotations

import msgspec
import pytest
from unittest.mock import AsyncMock, MagicMock, patch
from mcubridge.services.datastore import DatastoreComponent
from mcubridge.protocol.protocol import Command, DatastoreAction
from mcubridge.protocol.structures import DatastorePutPacket, DatastoreGetPacket, TopicRoute
from mcubridge.protocol.topics import Topic, topic_path

@pytest.fixture
def datastore_comp(runtime_config, runtime_state):
    serial_flow = MagicMock()
    serial_flow.send = AsyncMock(return_value=True)
    return DatastoreComponent(runtime_config, runtime_state, serial_flow)

@pytest.mark.asyncio
async def test_datastore_handle_put(datastore_comp):
    payload = msgspec.msgpack.encode(DatastorePutPacket(key="temp", value=b"25.5"))
    with patch("mcubridge.services.datastore.atomic_publish", new_callable=AsyncMock) as mock_publish:
        await datastore_comp.handle_put(0, payload)
        assert datastore_comp.state.datastore["temp"] == "25.5"
        mock_publish.assert_called_once()

@pytest.mark.asyncio
async def test_datastore_handle_get_request(datastore_comp):
    datastore_comp.state.datastore["version"] = "1.0.0"
    payload = msgspec.msgpack.encode(DatastoreGetPacket(key="version"))
    await datastore_comp.handle_get_request(0, payload)
    datastore_comp.serial_flow.send.assert_called_once()

@pytest.mark.asyncio
async def test_datastore_handle_mqtt_put(datastore_comp):
    route = TopicRoute("br/datastore/put/sys/uptime", "br", Topic.DATASTORE, ("put", "sys", "uptime"))
    msg = MagicMock()
    msg.payload = b"3600"
    await datastore_comp.handle_mqtt(route, msg)
    assert datastore_comp.state.datastore["sys/uptime"] == "3600"
    datastore_comp.serial_flow.send.assert_called_once()

@pytest.mark.asyncio
async def test_datastore_handle_mqtt_get(datastore_comp):
    datastore_comp.state.datastore["status"] = "OK"
    route = TopicRoute("br/datastore/get/status", "br", Topic.DATASTORE, ("get", "status"))
    msg = MagicMock()
    with patch("mcubridge.services.datastore.atomic_publish", new_callable=AsyncMock) as mock_publish:
        await datastore_comp.handle_mqtt(route, msg)
        mock_publish.assert_called_once()
