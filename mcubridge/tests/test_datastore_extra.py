"""Extra edge-case tests for DatastoreComponent (SIL-2)."""

from __future__ import annotations
from mcubridge.services.serial_flow import SerialFlowController
from mcubridge.transport.mqtt import MqttTransport
import msgspec

import os
import time
from unittest.mock import AsyncMock

import pytest
from mcubridge.config.settings import RuntimeConfig
from mcubridge.protocol.protocol import Status
from mcubridge.services.datastore import DatastoreComponent
from mcubridge.state.context import create_runtime_state
from mcubridge.protocol.topics import Topic
from mcubridge.protocol.protocol import DatastoreAction
from mcubridge.protocol.structures import TopicRoute
from aiomqtt.message import Message


@pytest.mark.asyncio
async def test_datastore_handle_put_malformed() -> None:
    config = RuntimeConfig(
        serial_shared_secret=b"secret_1234",
        file_system_root=os.path.abspath(
            f".tmp_tests/mcubridge-test-{os.getpid()}-{time.time_ns()}"
        ),
        allow_non_tmp_paths=True,
    )
    state = create_runtime_state(config)
    try:
        serial_flow = AsyncMock(spec=SerialFlowController)
        serial_flow.send = AsyncMock(return_value=True)
        mqtt_flow = AsyncMock(spec=MqttTransport)
        mqtt_flow.enqueue_mqtt = AsyncMock()

        comp = DatastoreComponent(config, state, serial_flow, mqtt_flow)
        result = await comp.handle_put(0, b"\xff\xff")
        assert result is False
        assert not mqtt_flow.enqueue_mqtt.called
    finally:
        state.cleanup()


@pytest.mark.asyncio
async def test_datastore_handle_get_malformed() -> None:
    config = RuntimeConfig(
        serial_shared_secret=b"secret_1234",
        file_system_root=os.path.abspath(
            f".tmp_tests/mcubridge-test-{os.getpid()}-{time.time_ns()}"
        ),
        allow_non_tmp_paths=True,
    )
    state = create_runtime_state(config)
    try:
        serial_flow = AsyncMock(spec=SerialFlowController)
        serial_flow.send = AsyncMock(return_value=True)
        mqtt_flow = AsyncMock(spec=MqttTransport)
        mqtt_flow.enqueue_mqtt = AsyncMock()

        comp = DatastoreComponent(config, state, serial_flow, mqtt_flow)
        result = await comp.handle_get_request(0, b"\xff\xff")
        assert result is False
        serial_flow.send.assert_called_once_with(
            Status.MALFORMED.value, b"data_get_malformed"
        )
        assert not mqtt_flow.enqueue_mqtt.called
    finally:
        state.cleanup()


@pytest.mark.asyncio
async def test_datastore_handle_get_truncation() -> None:
    config = RuntimeConfig(
        serial_shared_secret=b"secret_1234",
        file_system_root=os.path.abspath(
            f".tmp_tests/mcubridge-test-{os.getpid()}-{time.time_ns()}"
        ),
        allow_non_tmp_paths=True,
    )
    state = create_runtime_state(config)
    try:
        serial_flow = AsyncMock(spec=SerialFlowController)
        serial_flow.send = AsyncMock(return_value=True)
        mqtt_flow = AsyncMock(spec=MqttTransport)
        mqtt_flow.enqueue_mqtt = AsyncMock()

        comp = DatastoreComponent(config, state, serial_flow, mqtt_flow)
        state.datastore["long_key"] = "a" * 300

        from mcubridge.protocol.structures import DatastoreGetPacket

        payload = msgspec.msgpack.encode(DatastoreGetPacket(key="long_key"))

        result = await comp.handle_get_request(0, payload)
        assert result is True
        serial_flow.send.assert_called_once()
        assert mqtt_flow.enqueue_mqtt.called

        args, kwargs = mqtt_flow.enqueue_mqtt.call_args
        msg = args[0] if args else kwargs.get("message")
        pub_payload = msg.payload
        assert len(pub_payload) == 255
    finally:
        state.cleanup()


@pytest.mark.asyncio
async def test_datastore_handle_mqtt_edge_cases() -> None:
    config = RuntimeConfig(
        serial_shared_secret=b"secret_1234",
        file_system_root=os.path.abspath(
            f".tmp_tests/mcubridge-test-{os.getpid()}-{time.time_ns()}"
        ),
        allow_non_tmp_paths=True,
    )
    state = create_runtime_state(config)
    try:
        serial_flow = AsyncMock(spec=SerialFlowController)
        mqtt_flow = AsyncMock(spec=MqttTransport)
        mqtt_flow.enqueue_mqtt = AsyncMock()

        comp = DatastoreComponent(config, state, serial_flow, mqtt_flow)

        # 1. Empty route
        await comp.handle_mqtt(
            TopicRoute("br/d", "br", Topic.DATASTORE, ()),
            Message(topic="test/topic", payload=b"", qos=0, retain=False, mid=1, properties=None),
        )
        assert not mqtt_flow.enqueue_mqtt.called

        # 2. Unknown action
        await comp.handle_mqtt(
            TopicRoute("br/d/unknown/key", "br", Topic.DATASTORE, ("unknown", "key")),
            Message(topic="test/topic", payload=b"", qos=0, retain=False, mid=1, properties=None),
        )
        assert not mqtt_flow.enqueue_mqtt.called

        # 3. Missing key
        await comp.handle_mqtt(
            TopicRoute("br/d/put", "br", Topic.DATASTORE, (DatastoreAction.PUT.value,)),
            Message(topic="test/topic", payload=b"", qos=0, retain=False, mid=1, properties=None),
        )
        assert not mqtt_flow.enqueue_mqtt.called

        # 4. Echo suppression on GET
        state.datastore["echo_key"] = "val"
        await comp.handle_mqtt(
            TopicRoute("br/d/get/echo_key", "br", Topic.DATASTORE, (DatastoreAction.GET.value, "echo_key")),
            Message(topic="br/d/get/echo_key", payload=b"val", qos=0, retain=False, mid=1, properties=None),
        )
        assert not mqtt_flow.enqueue_mqtt.called

        # 5. Type coercion from int
        state.datastore["int_key"] = 42  # type: ignore
        await comp.handle_mqtt(
            TopicRoute("br/d/get/int_key/request", "br", Topic.DATASTORE, (DatastoreAction.GET.value, "int_key", "request")),
            Message(topic="test/topic", payload=b"", qos=0, retain=False, mid=1, properties=None),
        )
        # _publish_datastore_value publishes twice when reply_context is provided
        assert mqtt_flow.enqueue_mqtt.call_count == 2

    finally:
        state.cleanup()


@pytest.mark.asyncio
async def test_datastore_mqtt_put_too_large() -> None:
    config = RuntimeConfig(
        serial_shared_secret=b"secret_1234",
        file_system_root=os.path.abspath(
            f".tmp_tests/mcubridge-test-{os.getpid()}-{time.time_ns()}"
        ),
        allow_non_tmp_paths=True,
    )
    state = create_runtime_state(config)
    try:
        serial_flow = AsyncMock(spec=SerialFlowController)
        mqtt_flow = AsyncMock(spec=MqttTransport)
        mqtt_flow.enqueue_mqtt = AsyncMock()

        comp = DatastoreComponent(config, state, serial_flow, mqtt_flow)

        # Key too large
        long_key = "k" * 300
        await comp.handle_mqtt(
            TopicRoute(f"br/d/put/{long_key}", "br", Topic.DATASTORE, (DatastoreAction.PUT.value, long_key)),
            Message(topic="test/topic", payload=b"val", qos=0, retain=False, mid=1, properties=None),
        )
        assert not mqtt_flow.enqueue_mqtt.called
        assert long_key not in state.datastore

        # Value too large
        long_val = b"v" * 300
        await comp.handle_mqtt(
            TopicRoute("br/d/put/key", "br", Topic.DATASTORE, (DatastoreAction.PUT.value, "key")),
            Message(topic="test/topic", payload=long_val, qos=0, retain=False, mid=1, properties=None),
        )
        assert not mqtt_flow.enqueue_mqtt.called
        assert "key" not in state.datastore

    finally:
        state.cleanup()


@pytest.mark.asyncio
async def test_datastore_mqtt_get_too_large() -> None:
    config = RuntimeConfig(
        serial_shared_secret=b"secret_1234",
        file_system_root=os.path.abspath(
            f".tmp_tests/mcubridge-test-{os.getpid()}-{time.time_ns()}"
        ),
        allow_non_tmp_paths=True,
    )
    state = create_runtime_state(config)
    try:
        serial_flow = AsyncMock(spec=SerialFlowController)
        mqtt_flow = AsyncMock(spec=MqttTransport)
        mqtt_flow.enqueue_mqtt = AsyncMock()

        comp = DatastoreComponent(config, state, serial_flow, mqtt_flow)

        long_key = "k" * 300
        await comp.handle_mqtt(
            TopicRoute(f"br/d/get/{long_key}/request", "br", Topic.DATASTORE, (DatastoreAction.GET.value, long_key, "request")),
            Message(topic="test/topic", payload=b"", qos=0, retain=False, mid=1, properties=None),
        )
        assert not mqtt_flow.enqueue_mqtt.called

    finally:
        state.cleanup()
