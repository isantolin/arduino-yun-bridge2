"""Extra edge-case tests for DatastoreComponent (SIL-2)."""

from __future__ import annotations

import os
import time
from unittest.mock import AsyncMock, MagicMock

import pytest
from mcubridge.config.settings import RuntimeConfig
from mcubridge.protocol.protocol import Status
from mcubridge.services.datastore import DatastoreComponent
from mcubridge.state.context import create_runtime_state
from mcubridge.protocol.topics import Topic, TopicRoute
from mcubridge.protocol.protocol import DatastoreAction
from mcubridge.router.routers import MQTTRouter
from tests._helpers import make_mqtt_msg, make_route


@pytest.mark.asyncio
async def test_datastore_handle_put_malformed() -> None:
    config = RuntimeConfig(
        serial_shared_secret=b"secret_1234",
        file_system_root=f"/tmp/mcubridge-test-{os.getpid()}-{time.time_ns()}",
    )
    state = create_runtime_state(config)
    try:
        ctx = MagicMock()
        ctx.serial_flow = MagicMock()
        ctx.serial_flow.send = AsyncMock(return_value=True)
        ctx.mqtt_flow = MagicMock()
        ctx.mqtt_flow.publish = AsyncMock()

        comp = DatastoreComponent(config, state, ctx)
        result = await comp.handle_put(0, b"\xff\xff")
        assert result is False
        assert not ctx.mqtt_flow.publish.called
    finally:
        state.cleanup()


@pytest.mark.asyncio
async def test_datastore_handle_get_malformed() -> None:
    config = RuntimeConfig(
        serial_shared_secret=b"secret_1234",
        file_system_root=f"/tmp/mcubridge-test-{os.getpid()}-{time.time_ns()}",
    )
    state = create_runtime_state(config)
    try:
        ctx = MagicMock()
        ctx.serial_flow = MagicMock()
        ctx.serial_flow.send = AsyncMock(return_value=True)
        ctx.mqtt_flow = MagicMock()
        ctx.mqtt_flow.publish = AsyncMock()

        comp = DatastoreComponent(config, state, ctx)
        result = await comp.handle_get_request(0, b"\xff\xff")
        assert result is False
        ctx.serial_flow.send.assert_called_once_with(Status.MALFORMED.value, b"data_get_malformed")
        assert not ctx.mqtt_flow.publish.called
    finally:
        state.cleanup()


@pytest.mark.asyncio
async def test_datastore_handle_get_truncation() -> None:
    config = RuntimeConfig(
        serial_shared_secret=b"secret_1234",
        file_system_root=f"/tmp/mcubridge-test-{os.getpid()}-{time.time_ns()}",
    )
    state = create_runtime_state(config)
    try:
        ctx = MagicMock()
        ctx.serial_flow = MagicMock()
        ctx.serial_flow.send = AsyncMock(return_value=True)
        ctx.mqtt_flow = MagicMock()
        ctx.mqtt_flow.publish = AsyncMock()

        comp = DatastoreComponent(config, state, ctx)
        state.datastore["long_key"] = "a" * 300

        from mcubridge.protocol.structures import DatastoreGetPacket
        payload = DatastoreGetPacket(key="long_key").encode()

        result = await comp.handle_get_request(0, payload)
        assert result is True
        ctx.serial_flow.send.assert_called_once()
        assert ctx.mqtt_flow.publish.called

        args, kwargs = ctx.mqtt_flow.publish.call_args
        pub_payload = kwargs.get("payload") or args[1]
        assert len(pub_payload) == 255
    finally:
        state.cleanup()


@pytest.mark.asyncio
async def test_datastore_handle_mqtt_edge_cases() -> None:
    config = RuntimeConfig(
        serial_shared_secret=b"secret_1234",
        file_system_root=f"/tmp/mcubridge-test-{os.getpid()}-{time.time_ns()}",
    )
    state = create_runtime_state(config)
    try:
        ctx = MagicMock()
        ctx.serial_flow = MagicMock()
        ctx.mqtt_flow = MagicMock()
        ctx.mqtt_flow.publish = AsyncMock()

        comp = DatastoreComponent(config, state, ctx)
        router = MQTTRouter()
        router.register(Topic.DATASTORE, comp.handle_mqtt_put, action=DatastoreAction.PUT)
        router.register(Topic.DATASTORE, comp.handle_mqtt_get, action=DatastoreAction.GET)

        # 1. No segments -> action is None -> not dispatched
        route1 = TopicRoute("br/d", "br", Topic.DATASTORE, ())
        assert not await router.dispatch(route1, make_mqtt_msg(b""))

        # 2. Unknown action -> not dispatched
        await router.dispatch(make_route(Topic.DATASTORE, "unknown", "key"), make_mqtt_msg(b""))
        assert not ctx.mqtt_flow.publish.called

        # 3. Missing key (handled inside handler)
        await router.dispatch(make_route(Topic.DATASTORE, DatastoreAction.PUT.value), make_mqtt_msg(b""))
        assert not ctx.mqtt_flow.publish.called

        # 4. Echo suppression on GET (handled inside handler)
        state.datastore["echo_key"] = "val"
        await router.dispatch(
            make_route(Topic.DATASTORE, DatastoreAction.GET.value, "echo_key"),
            make_mqtt_msg(b"val")
        )
        assert not ctx.mqtt_flow.publish.called

        # 5. Type coercion from int
        state.datastore["int_key"] = 42 # type: ignore
        await router.dispatch(
            make_route(Topic.DATASTORE, DatastoreAction.GET.value, "int_key", "request"),
            make_mqtt_msg(b"")
        )
        # _publish_datastore_value publishes twice when reply_context is provided
        assert ctx.mqtt_flow.publish.call_count == 2

    finally:
        state.cleanup()


@pytest.mark.asyncio
async def test_datastore_mqtt_put_too_large() -> None:
    config = RuntimeConfig(
        serial_shared_secret=b"secret_1234",
        file_system_root=f"/tmp/mcubridge-test-{os.getpid()}-{time.time_ns()}",
    )
    state = create_runtime_state(config)
    try:
        ctx = MagicMock()
        ctx.serial_flow = MagicMock()
        ctx.mqtt_flow = MagicMock()
        ctx.mqtt_flow.publish = AsyncMock()

        comp = DatastoreComponent(config, state, ctx)

        # Key too large
        long_key = "k" * 300
        route = make_route(Topic.DATASTORE, DatastoreAction.PUT.value, long_key)
        await comp.handle_mqtt_put(route, make_mqtt_msg(b"val"))
        assert not ctx.mqtt_flow.publish.called
        assert long_key not in state.datastore

        # Value too large
        long_val = b"v" * 300
        route2 = make_route(Topic.DATASTORE, DatastoreAction.PUT.value, "key")
        await comp.handle_mqtt_put(route2, make_mqtt_msg(long_val))
        assert not ctx.mqtt_flow.publish.called
        assert "key" not in state.datastore

    finally:
        state.cleanup()


@pytest.mark.asyncio
async def test_datastore_mqtt_get_too_large() -> None:
    config = RuntimeConfig(
        serial_shared_secret=b"secret_1234",
        file_system_root=f"/tmp/mcubridge-test-{os.getpid()}-{time.time_ns()}",
    )
    state = create_runtime_state(config)
    try:
        ctx = MagicMock()
        ctx.serial_flow = MagicMock()
        ctx.mqtt_flow = MagicMock()
        ctx.mqtt_flow.publish = AsyncMock()

        comp = DatastoreComponent(config, state, ctx)

        long_key = "k" * 300
        route = make_route(Topic.DATASTORE, DatastoreAction.GET.value, long_key, "request")
        await comp.handle_mqtt_get(route, make_mqtt_msg(b""))
        assert not ctx.mqtt_flow.publish.called

    finally:
        state.cleanup()
