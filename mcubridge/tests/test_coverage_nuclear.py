import asyncio
import pytest
from unittest.mock import AsyncMock, MagicMock, patch
import msgspec
import time
from aiomqtt.message import Message
from mcubridge.services.runtime import BridgeService
from mcubridge.services.handshake import SerialHandshakeManager, derive_serial_timing
from mcubridge.transport.serial import SerialTransport
from mcubridge.state.context import RuntimeState, create_runtime_state
from mcubridge.state.metrics import DaemonMetrics
from mcubridge.config.settings import RuntimeConfig
from mcubridge.protocol import protocol
from mcubridge.protocol.frame import Frame
from mcubridge.protocol.topics import TopicRoute
from mcubridge.protocol.protocol import DatastoreAction, SystemAction, Topic, Status, Command
from mcubridge.protocol.structures import (
    AckPacket, VersionResponsePacket, FreeMemoryResponsePacket,
    QueuedPublish, SpiTransferResponsePacket
)

@pytest.fixture
def config():
    return RuntimeConfig(
        mqtt_topic="br",
        serial_port="/dev/ttytest",
        serial_baud=115200,
        serial_safe_baud=9600,
        mqtt_host="localhost",
        mqtt_port=1883,
        serial_shared_secret=b"test_secret_1234567890123456"
    )

@pytest.fixture
def state(config):
    return create_runtime_state(config)

@pytest.fixture
def mock_serial():
    m = AsyncMock(spec=SerialTransport)
    m.send = AsyncMock(return_value=True)
    async def send_wait_side_effect(cmd, payload):
        if cmd == Command.CMD_GET_VERSION.value:
            return msgspec.msgpack.encode(VersionResponsePacket(major=1, minor=2, patch=3))
        if cmd == Command.CMD_GET_FREE_MEMORY.value:
            return msgspec.msgpack.encode(FreeMemoryResponsePacket(value=1024))
        if cmd == Command.CMD_SPI_TRANSFER.value:
            return msgspec.msgpack.encode(SpiTransferResponsePacket(data=b"\x00\x00"))
        return None
    m.send_and_wait_payload = AsyncMock(side_effect=send_wait_side_effect)
    m.acknowledge = AsyncMock()
    return m

@pytest.fixture
def mock_mqtt():
    m = AsyncMock()
    m.enqueue_mqtt = AsyncMock()
    return m

@pytest.mark.asyncio
async def test_runtime_coverage_boost(config, state, mock_serial, mock_mqtt):
    service = BridgeService(config, state, mock_serial, mock_mqtt)
    state.link_sync_event.set()
    state.serial_tx_allowed.set()
    state.mark_synchronized()
    
    # Test MQTT system handlers
    route = TopicRoute(raw="br/sys/bootloader", prefix="br", topic=Topic.SYSTEM, segments=(SystemAction.BOOTLOADER,))
    msg = Message(topic="br/sys/bootloader", payload=b"", qos=0, retain=False, mid=1, properties=None)
    await service._handle_mqtt_system(route, msg)
    
    route = TopicRoute(raw="br/sys/free_memory/get", prefix="br", topic=Topic.SYSTEM, segments=(SystemAction.FREE_MEMORY, SystemAction.GET))
    await service._handle_mqtt_system(route, msg)
    
    route = TopicRoute(raw="br/sys/version/get", prefix="br", topic=Topic.SYSTEM, segments=(SystemAction.VERSION, SystemAction.GET))
    await service._handle_mqtt_system(route, msg)
    
    # Test DataStore handlers
    route = TopicRoute(raw="br/ds/some/key", prefix="br", topic=Topic.DATASTORE, segments=(DatastoreAction.PUT, "some", "key"))
    msg = Message(topic="br/ds/some/key", payload=b"val", qos=0, retain=False, mid=1, properties=None)
    await service._handle_mqtt_datastore(route, msg)
    
    route = TopicRoute(raw="br/ds/some/key/request", prefix="br", topic=Topic.DATASTORE, segments=(DatastoreAction.GET, "some", "key", "request"))
    await service._handle_mqtt_datastore(route, msg)
    
    # Test Pin Handlers
    msg = Message(topic="br/dw/13", payload=b"1", qos=0, retain=False, mid=1, properties=None)
    await service.handle_mqtt_message(msg)
    msg = Message(topic="br/aw/11", payload=b"128", qos=0, retain=False, mid=1, properties=None)
    await service.handle_mqtt_message(msg)
    msg = Message(topic="br/pm/13", payload=b"1", qos=0, retain=False, mid=1, properties=None)
    await service.handle_mqtt_message(msg)

    # Test MCU Handlers
    await service.handle_mcu_frame(protocol.Command.CMD_GET_VERSION_RESP.value, 1, msgspec.msgpack.encode(VersionResponsePacket(1,0,0)))
    await service.handle_mcu_frame(protocol.Command.CMD_GET_FREE_MEMORY_RESP.value, 2, msgspec.msgpack.encode(FreeMemoryResponsePacket(1024)))
    await service.handle_mcu_frame(protocol.Command.CMD_XOFF.value, 0, b"")
    await service.handle_mcu_frame(protocol.Command.CMD_XON.value, 0, b"")
    await service.handle_mcu_frame(Status.ACK.value, 1, b"\x01")

    # Test _handle_mcu_status
    await service._handle_mcu_status(1, Status.ERROR, b"error")

    # Test file handlers
    msg = Message(topic="br/file/read/test.txt/request", payload=b"", qos=0, retain=False, mid=1, properties=None)
    await service.handle_mqtt_message(msg)
    msg = Message(topic="br/file/write/test.txt", payload=b"content", qos=0, retain=False, mid=1, properties=None)
    await service.handle_mqtt_message(msg)
    msg = Message(topic="br/file/remove/test.txt", payload=b"", qos=0, retain=False, mid=1, properties=None)
    await service.handle_mqtt_message(msg)

    # Test Console handler
    msg = Message(topic="br/console/in", payload=b"hello", qos=0, retain=False, mid=1, properties=None)
    await service.handle_mqtt_message(msg)

    # Test Process handlers
    msg = Message(topic="br/proc/run/ls", payload=b"", qos=0, retain=False, mid=1, properties=None)
    await service.handle_mqtt_message(msg)
    msg = Message(topic="br/proc/kill/123", payload=b"", qos=0, retain=False, mid=1, properties=None)
    await service.handle_mqtt_message(msg)

    # Test SPI handler
    msg = Message(topic="br/spi/transfer", payload=b"\x01\x02", qos=0, retain=False, mid=1, properties=None)
    await service.handle_mqtt_message(msg)

@pytest.mark.asyncio
async def test_handshake_coverage_boost(config, state, mock_serial, mock_mqtt):
    timing = derive_serial_timing(config)
    hm = SerialHandshakeManager(
        config=config, 
        state=state, 
        serial_timing=timing,
        send_frame=mock_serial.send, 
        enqueue_mqtt=mock_mqtt.enqueue_mqtt, 
        acknowledge_frame=mock_serial.acknowledge
    )
    
    mock_serial.send.return_value = False
    try:
        async with asyncio.timeout(0.1):
            await hm.synchronize()
    except asyncio.TimeoutError:
        pass
    
    await hm.handle_link_sync_resp(1, b"\xff")
    with patch("mcubridge.security.security.verify_crypto_integrity", return_value=True):
        payload = b"\x00" * 16 # nonce
        payload += b"\x01" * 16 # tag
        await hm.handle_link_sync_resp(1, payload)

@pytest.mark.asyncio
async def test_serial_transport_coverage_boost(config, state):
    mock_loop = MagicMock()
    transport = SerialTransport(config, state, mock_loop)
    corrupt_data = b"\xaa\xbb\xcc\xdd"
    try:
        await transport._async_process_packet_with_limit(corrupt_data)
    except Exception:
        pass
    with patch.object(transport, "_send_raw", AsyncMock()):
        await transport.acknowledge(1, 1)

@pytest.mark.asyncio
async def test_state_context_coverage_boost(config, state):
    state.mark_transport_connected()
    state.mark_transport_disconnected()
    state.mark_synchronized()
    state.last_watchdog_beat = time.time()
    state.mark_supervisor_healthy("test")
    state.build_metrics_snapshot()
    state.build_bridge_snapshot()
    state.cleanup()

def test_daemon_entry_coverage():
    from mcubridge.daemon import main
    with patch("sys.argv", ["mcubridge", "--debug"]):
        with patch("asyncio.run") as mock_run:
            main(overrides={})
            mock_run.assert_called()
