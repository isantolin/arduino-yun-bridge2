import asyncio
from unittest.mock import AsyncMock, MagicMock, patch
from pathlib import Path

import pytest
import msgspec
from aiomqtt.message import Message

# pyright: reportPrivateUsage=false
from mcubridge.services.runtime import BridgeService
from mcubridge.transport.serial import SerialTransport
from mcubridge.transport.mqtt import MqttTransport
from mcubridge.state.context import create_runtime_state
from mcubridge.config.settings import RuntimeConfig
from mcubridge.protocol.protocol import Topic, FileAction, Command, Status, SystemAction
from mcubridge.protocol.structures import TopicRoute, FileWritePacket, VersionResponsePacket, FreeMemoryResponsePacket


@pytest.fixture
def service_setup(tmp_path):
    config = RuntimeConfig(mqtt_topic="br", serial_port="/dev/test", file_system_root=str(tmp_path))
    state = create_runtime_state(config)
    serial = AsyncMock()
    mqtt = AsyncMock()
    service = BridgeService(config, state, serial, mqtt)
    return service, state, serial


@pytest.mark.asyncio
async def test_runtime_file_loop_coverage_v4(service_setup):
    service, state, _ = service_setup
    
    # Target L748-789: MQTT File loop with deep path
    route = TopicRoute(
        raw="br/file/write/a/b/c/test.txt",
        prefix="br",
        topic=Topic.FILE,
        segments=(FileAction.WRITE, "a", "b", "c", "test.txt")
    )
    msg = Message(topic="br/file/write/a/b/c/test.txt", payload=b"data", qos=0, retain=False, mid=1, properties=None)
    
    await service._handle_mqtt_file(route, msg)
    assert (Path(state.file_system_root) / "a/b/c/test.txt").read_bytes() == b"data"


@pytest.mark.asyncio
async def test_mqtt_publish_direct_mock(service_setup):
    service, state, _ = service_setup
    transport = MqttTransport(service.config, state)
    
    # Target direct publish in enqueue_mqtt
    mock_client = AsyncMock()
    transport._client = mock_client
    
    from mcubridge.protocol.structures import QueuedPublish
    item = QueuedPublish(topic_name="test", payload=b"data")
    
    await transport.enqueue_mqtt(item)
    assert mock_client.publish.called


@pytest.mark.asyncio
async def test_runtime_system_mqtt_success_v3(service_setup):
    service, state, serial = service_setup
    state.mark_synchronized()
    
    # Target _handle_mqtt_system success paths
    msg = Message(topic="br/sys/get/version", payload=b"", qos=0, retain=False, mid=1, properties=None)
    serial.send_and_wait_payload.return_value = msgspec.msgpack.encode(VersionResponsePacket(major=2, minor=0, patch=0))
    await service.handle_mqtt_message(msg)
    
    msg = Message(topic="br/sys/get/free_memory", payload=b"", qos=0, retain=False, mid=2, properties=None)
    serial.send_and_wait_payload.return_value = msgspec.msgpack.encode(FreeMemoryResponsePacket(value=1024))
    await service.handle_mqtt_message(msg)
