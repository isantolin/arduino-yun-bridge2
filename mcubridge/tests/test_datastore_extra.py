"""Extra edge-case tests for DatastoreComponent (SIL-2)."""

from __future__ import annotations

from unittest.mock import AsyncMock

import pytest
from mcubridge.config.settings import RuntimeConfig
from mcubridge.services.datastore import DatastoreComponent
from mcubridge.services.serial_flow import SerialFlowController
from mcubridge.state.context import create_runtime_state
from mcubridge.protocol.topics import Topic
from mcubridge.protocol.structures import TopicRoute
from aiomqtt.message import Message


@pytest.mark.asyncio
async def test_datastore_handle_put_malformed(
    runtime_config: RuntimeConfig,
) -> None:
    state = create_runtime_state(runtime_config)
    try:
        serial_flow = AsyncMock(spec=SerialFlowController)
        enqueue_mqtt = AsyncMock()

        comp = DatastoreComponent(runtime_config, state, serial_flow, enqueue_mqtt)

        # 1. Invalid msgpack
        result = await comp.handle_put(1, b"\xc1")
        assert result is False
    finally:
        state.cleanup()


@pytest.mark.asyncio
async def test_datastore_handle_get_request_malformed(
    runtime_config: RuntimeConfig,
) -> None:
    state = create_runtime_state(runtime_config)
    try:
        serial_flow = AsyncMock(spec=SerialFlowController)
        enqueue_mqtt = AsyncMock()

        comp = DatastoreComponent(runtime_config, state, serial_flow, enqueue_mqtt)

        # 1. Invalid msgpack
        result = await comp.handle_get_request(1, b"\xc1")
        assert result is False
    finally:
        state.cleanup()


@pytest.mark.asyncio
async def test_datastore_handle_mqtt_missing_key(
    runtime_config: RuntimeConfig,
) -> None:
    state = create_runtime_state(runtime_config)
    try:
        serial_flow = AsyncMock(spec=SerialFlowController)
        enqueue_mqtt = AsyncMock()

        comp = DatastoreComponent(runtime_config, state, serial_flow, enqueue_mqtt)

        # Route with missing key
        route = TopicRoute(
            raw="br/ds/get", prefix="br", topic=Topic.DATASTORE, segments=("get",)
        )
        msg = AsyncMock(spec=Message)

        result = await comp.handle_mqtt(route, msg)
        assert result is True
        assert enqueue_mqtt.called
    finally:
        state.cleanup()
