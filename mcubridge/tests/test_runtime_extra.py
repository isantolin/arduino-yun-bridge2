"""Extra coverage for mcubridge.services.runtime."""

from __future__ import annotations

import os
import time
from unittest.mock import AsyncMock, MagicMock

import pytest

from mcubridge.config.settings import RuntimeConfig
from mcubridge.services.runtime import BridgeService
from mcubridge.state.context import create_runtime_state


@pytest.mark.asyncio
async def test_runtime_on_serial_connected_errors() -> None:
    config = RuntimeConfig(
        serial_shared_secret=b"secret_1234",
        file_system_root=os.path.abspath(
            f".tmp_tests/mcubridge-test-{os.getpid()}-{time.time_ns()}"
        ),
        allow_non_tmp_paths=True,
    )
    state = create_runtime_state(config)
    try:
        mqtt_mock = MagicMock()
        mqtt_mock.enqueue_mqtt = AsyncMock()
        mqtt_mock.enqueue_mqtt = AsyncMock()

        service = BridgeService(config, state, mqtt_mock)

        # Side effect to update state when synchronize is called
        async def _mock_sync():
            state.mark_synchronized()
            return True

        service.handshake_manager.synchronize = AsyncMock(side_effect=_mock_sync)

        system = service.system
        console = service.console

        import warnings

        # 1. Error requesting version
        system.request_mcu_version = AsyncMock(side_effect=RuntimeError("fail"))
        with warnings.catch_warnings():
            warnings.filterwarnings("ignore", category=UserWarning)
            await service.on_serial_connected()
        assert system.request_mcu_version.called

        # 2. Error flushing console
        console.flush_queue = AsyncMock(side_effect=ValueError("boom"))
        with warnings.catch_warnings():
            warnings.filterwarnings("ignore", category=UserWarning)
            await service.on_serial_connected()
        assert console.flush_queue.called
    finally:
        state.cleanup()


@pytest.mark.asyncio
async def test_runtime_handle_mqtt_message_dispatch_error() -> None:
    config = RuntimeConfig(
        serial_shared_secret=b"secret_1234", allow_non_tmp_paths=True
    )
    state = create_runtime_state(config)
    try:
        mqtt_mock = MagicMock()
        mqtt_mock.enqueue_mqtt = AsyncMock()
        mqtt_mock.enqueue_mqtt = AsyncMock()

        service = BridgeService(config, state, mqtt_mock)

        from aiomqtt.message import Message

        msg = Message(
            topic="br/system/status",
            payload=b"{}",
            qos=0,
            retain=False,
            mid=1,
            properties=None,
        )

        # Mock dispatcher.dispatch_mqtt_message to see if it's called
        service.dispatcher.dispatch_mqtt_message = AsyncMock(
            side_effect=IndexError("bad dispatch")
        )

        with pytest.raises(IndexError, match="bad dispatch"):
            await service.handle_mqtt_message(msg)

    finally:
        state.cleanup()


@pytest.mark.asyncio
async def test_runtime_reject_topic_action_properties() -> None:
    config = RuntimeConfig(
        serial_shared_secret=b"secret_1234", allow_non_tmp_paths=True
    )
    state = create_runtime_state(config)
    try:
        mqtt_mock = MagicMock()
        mqtt_mock.enqueue_mqtt = AsyncMock()
        mqtt_mock.enqueue_mqtt = AsyncMock()

        service = BridgeService(config, state, mqtt_mock)
        from mcubridge.protocol.topics import Topic
        from aiomqtt.message import Message
        from paho.mqtt.properties import Properties
        from paho.mqtt.packettypes import PacketTypes

        props = Properties(PacketTypes.PUBLISH)
        props.ResponseTopic = "resp"
        props.CorrelationData = b"cid"
        inbound = Message(
            topic="br/system/cmd",
            payload=b"",
            qos=0,
            retain=False,
            mid=1,
            properties=props,
        )

        await service._reject_topic_action(inbound, Topic.SYSTEM, "action")  # type: ignore[reportPrivateUsage]

        # Should have called enqueue_mqtt
        assert service.mqtt_flow.enqueue_mqtt.called
        # Fix: checking kwargs accurately
        _, kwargs = service.mqtt_flow.enqueue_mqtt.call_args
        assert kwargs.get("reply_context") is inbound
    finally:
        state.cleanup()


@pytest.mark.asyncio
async def test_runtime_publish_bridge_snapshot_handshake() -> None:
    config = RuntimeConfig(
        serial_shared_secret=b"secret_1234", allow_non_tmp_paths=True
    )
    state = create_runtime_state(config)
    try:
        mqtt_mock = MagicMock()
        mqtt_mock.enqueue_mqtt = AsyncMock()
        mqtt_mock.enqueue_mqtt = AsyncMock()

        service = BridgeService(config, state, mqtt_mock)

        from aiomqtt.message import Message
        from paho.mqtt.properties import Properties
        from paho.mqtt.packettypes import PacketTypes

        props = Properties(PacketTypes.PUBLISH)
        props.ResponseTopic = "reply"
        inbound = Message(
            topic="br/s/b/h/get",
            payload=b"",
            qos=0,
            retain=False,
            mid=1,
            properties=props,
        )

        await service._publish_bridge_snapshot("handshake", inbound)  # type: ignore[reportPrivateUsage]

        assert service.mqtt_flow.enqueue_mqtt.called
    finally:
        state.cleanup()
