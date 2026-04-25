"""Extra tests for DatastoreComponent edges."""

from __future__ import annotations

import os
import time
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import msgspec
import pytest
from mcubridge.config.settings import RuntimeConfig
from mcubridge.protocol.protocol import DatastoreAction
from mcubridge.protocol.structures import DatastoreGetPacket, DatastorePutPacket, TopicRoute
from mcubridge.protocol.topics import Topic
from mcubridge.services.datastore import DatastoreComponent
from mcubridge.state.context import create_runtime_state


def make_mqtt_msg(payload: bytes) -> MagicMock:
    msg = MagicMock()
    msg.payload = payload
    return msg


def make_route(topic: Topic, action: str, *segments: str) -> TopicRoute:
    all_segments = (action,) + segments
    raw = f"br/{topic.value}/{'/'.join(all_segments)}"
    return TopicRoute(raw, "br", topic, all_segments)


@pytest.mark.asyncio
async def test_datastore_handle_get_truncation() -> None:
    # Use a secure secret to pass validation
    config = RuntimeConfig(
        serial_shared_secret=b"secure_secret_1234",
        file_system_root=f".tmp_tests/mcubridge-test-{os.getpid()}-{time.time_ns()}",
    )
    state = create_runtime_state(config)
    try:
        serial_flow = AsyncMock()
        serial_flow.send = AsyncMock(return_value=True)

        comp = DatastoreComponent(config, state, serial_flow)
        state.datastore["long_key"] = "a" * 300

        from mcubridge.protocol.structures import DatastoreGetPacket
        payload = msgspec.msgpack.encode(DatastoreGetPacket(key="long_key"))

        # handle_get_request sends to MCU, it does NOT publish to MQTT directly
        result = await comp.handle_get_request(0, payload)
        assert result is True
        assert serial_flow.send.called
    finally:
        if os.path.exists(config.file_system_root):
            import shutil
            shutil.rmtree(config.file_system_root, ignore_errors=True)


@pytest.mark.asyncio
async def test_datastore_handle_mqtt_edge_cases() -> None:
    config = RuntimeConfig(serial_shared_secret=b"secure_secret_1234")
    state = create_runtime_state(config)
    serial_flow = AsyncMock()
    comp = DatastoreComponent(config, state, serial_flow)

    with patch("mcubridge.services.datastore.atomic_publish", new_callable=AsyncMock) as mock_pub:
        # 1. Empty route
        await comp.handle_mqtt(TopicRoute("br/d", "br", Topic.DATASTORE, ()), make_mqtt_msg(b""))
        assert not mock_pub.called

        # 2. Unknown action
        await comp.handle_mqtt(make_route(Topic.DATASTORE, "unknown", "key"), make_mqtt_msg(b""))
        assert not mock_pub.called

        # 3. Missing key
        await comp.handle_mqtt(make_route(Topic.DATASTORE, DatastoreAction.PUT.value), make_mqtt_msg(b""))
        assert not mock_pub.called


@pytest.mark.asyncio
async def test_datastore_mqtt_put_too_large() -> None:
    config = RuntimeConfig(serial_shared_secret=b"secure_secret_1234")
    state = create_runtime_state(config)
    serial_flow = AsyncMock()
    comp = DatastoreComponent(config, state, serial_flow)

    # Missing segments test
    route = TopicRoute("br/datastore/put", "br", Topic.DATASTORE, ("put",))
    result = await comp.handle_mqtt(route, make_mqtt_msg(b"val"))
    assert result is False
