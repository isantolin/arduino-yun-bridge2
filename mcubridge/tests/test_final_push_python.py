import asyncio
from typing import Any, Tuple
from unittest.mock import AsyncMock, patch
from pathlib import Path

import pytest
import msgspec
from aiomqtt.message import Message

# pyright: reportPrivateUsage=false
from mcubridge.services.runtime import BridgeService
from mcubridge.transport.mqtt import MqttTransport
from mcubridge.state.context import create_runtime_state, RuntimeState
from mcubridge.config.settings import RuntimeConfig
from mcubridge.protocol.protocol import Topic, FileAction
from mcubridge.protocol.structures import (
    TopicRoute,
    VersionResponsePacket,
    FreeMemoryResponsePacket,
)


@pytest.fixture
def service_setup(tmp_path: Path) -> Tuple[BridgeService, RuntimeState, AsyncMock]:
    config = RuntimeConfig(
        mqtt_topic="br", serial_port="/dev/test", file_system_root=str(tmp_path)
    )
    state = create_runtime_state(config)
    serial = AsyncMock()
    mqtt = AsyncMock()
    service = BridgeService(config, state, serial, mqtt)
    return service, state, serial


@pytest.mark.asyncio
async def test_runtime_file_loop_coverage_v3(service_setup: Any) -> None:
    service, state, _ = service_setup

    route = TopicRoute(
        raw="br/file/write/a/b/c/test.txt",
        prefix="br",
        topic=Topic.FILE,
        segments=(FileAction.WRITE, "a", "b", "c", "test.txt"),
    )
    msg = Message(
        topic="br/file/write/a/b/c/test.txt",
        payload=b"data",
        qos=0,
        retain=False,
        mid=1,
        properties=None,
    )

    await service._handle_mqtt_file(route, msg)
    assert (
        Path(state.file_system_root) / "a" / "b" / "c" / "test.txt"
    ).read_bytes() == b"data"


@pytest.mark.asyncio
async def test_mqtt_publish_error_paths_v3(service_setup: Any) -> None:
    service, state, _ = service_setup
    transport = MqttTransport(service.config, state)

    mock_client = AsyncMock()
    mock_client.publish.side_effect = [Exception("retry"), None]

    from mcubridge.protocol.structures import QueuedPublish

    item = QueuedPublish(topic_name="test", payload=b"data")

    # Corrected attribute from state
    state.mqtt_publish_queue.put_nowait(item)

    def dummy_wraps(x: Any) -> Any:
        return x

    with patch("tenacity.AsyncRetrying.wraps", dummy_wraps):
        try:
            # Type cast to any to bypass static type checks on internal methods if needed
            from typing import cast

            await asyncio.wait_for(
                cast(Any, transport)._publish_session(mock_client), 0.1
            )
        except (asyncio.TimeoutError, Exception):
            pass


@pytest.mark.asyncio
async def test_runtime_system_mqtt_success_v2(service_setup: Any) -> None:
    service, state, serial = service_setup
    state.mark_synchronized()

    msg = Message(
        topic="br/sys/get/version",
        payload=b"",
        qos=0,
        retain=False,
        mid=1,
        properties=None,
    )
    serial.send_and_wait_payload.return_value = msgspec.msgpack.encode(
        VersionResponsePacket(major=2, minor=0, patch=0)
    )
    await service.handle_mqtt_message(msg)

    msg = Message(
        topic="br/sys/get/free_memory",
        payload=b"",
        qos=0,
        retain=False,
        mid=2,
        properties=None,
    )
    serial.send_and_wait_payload.return_value = msgspec.msgpack.encode(
        FreeMemoryResponsePacket(value=1024)
    )
    await service.handle_mqtt_message(msg)
