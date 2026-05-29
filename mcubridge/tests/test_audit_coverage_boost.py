import asyncio
import time
from unittest.mock import MagicMock, patch, AsyncMock
from pathlib import Path
import pytest
import msgspec
from mcubridge.services.runtime import BridgeService
from mcubridge.config.settings import RuntimeConfig
from mcubridge.state.context import create_runtime_state
from mcubridge.protocol import protocol, mcubridge_pb2 as pb
from mcubridge.protocol.structures import QueuedPublish, SerialTimingWindow
from mcubridge.services.handshake import SerialHandshakeManager, derive_serial_timing

@pytest.fixture
def mock_bridge():
    config = RuntimeConfig(
        mqtt_topic="br",
        serial_port="/dev/ttytest",
        serial_baud=9600,
        mqtt_host="localhost",
        mqtt_port=1883,
        mqtt_spool_dir="/tmp/mcubridge_spool_test"
    )
    state = create_runtime_state(config)
    serial = MagicMock()
    serial.send = AsyncMock(return_value=True)
    service = BridgeService(config, state, serial)
    return service

def test_resolve_reply_message_coverage(mock_bridge):
    service = mock_bridge
    msg = QueuedPublish(topic_name="original/topic", payload=b"data")
    res = service._resolve_reply_message(msg, None)
    assert res.topic_name == "original/topic"
    
    reply_ctx = MagicMock()
    reply_ctx.properties.ResponseTopic = "reply/topic"
    reply_ctx.properties.CorrelationData = b"corr123"
    reply_ctx.topic = "request/topic"
    res = service._resolve_reply_message(msg, reply_ctx)
    assert res.topic_name == "reply/topic"
    assert res.correlation_data == b"corr123"

def test_record_mqtt_drop_coverage(mock_bridge):
    service = mock_bridge
    service._record_mqtt_drop("test/topic")
    assert service.state.mqtt_dropped_messages == 1

def test_list_mqtt_spool_files_coverage(mock_bridge, tmp_path):
    service = mock_bridge
    service.config.mqtt_spool_dir = str(tmp_path)
    assert service._list_mqtt_spool_files() == []
    f1 = tmp_path / "1.msgpack"
    f1.write_bytes(b"data")
    files = service._list_mqtt_spool_files()
    assert len(files) == 1

@pytest.mark.asyncio
async def test_gen_handler_decode_error(mock_bridge):
    service = mock_bridge
    handler = service._gen_handler(pb.ConsoleWrite, MagicMock())
    res = await handler(1, b"not a protobuf")
    assert res is False

@pytest.mark.asyncio
async def test_on_mcu_ack_decode_error(mock_bridge):
    service = mock_bridge
    with patch("mcubridge.services.runtime.logger") as mock_logger:
        await service._on_mcu_ack(1, b"invalid")
        assert mock_logger.warning.called

@pytest.mark.asyncio
async def test_handle_mcu_status_decode_error(mock_bridge):
    service = mock_bridge
    from mcubridge.protocol.protocol import Status
    service._publish_mqtt_message = AsyncMock(return_value=False)
    service._spool_mqtt_message_locked = AsyncMock(return_value=True)
    await service._handle_mcu_status(1, Status.OK, b"\xff\xff\xff")
    assert service._publish_mqtt_message.called

def test_mqtt_spool_health_coverage(mock_bridge):
    service = mock_bridge
    service._mark_mqtt_spool_failure("disk full")
    assert service.state.mqtt_spool_degraded is True
    service._mark_mqtt_spool_healthy(5)
    assert service.state.mqtt_spool_degraded is False

from mcubridge.transport.serial import SerialTransport

@pytest.mark.asyncio
async def test_serial_transport_active_failure(mock_bridge):
    service = mock_bridge
    transport = SerialTransport(service.config, service.state, service)
    with pytest.raises(RuntimeError, match="Serial writer inactive"):
        transport._active_transport()

@pytest.mark.asyncio
async def test_serial_transport_reset_coverage(mock_bridge):
    service = mock_bridge
    transport = SerialTransport(service.config, service.state, service)
    from mcubridge.protocol.structures import PendingCommand
    mock_cmd = MagicMock(spec=PendingCommand)
    transport._current = mock_cmd
    await transport.reset()
    mock_cmd.mark_failure.assert_called()

@pytest.mark.asyncio
async def test_serial_transport_stop_coverage(mock_bridge):
    service = mock_bridge
    transport = SerialTransport(service.config, service.state, service)
    mock_writer = MagicMock()
    transport.writer = mock_writer
    await transport.stop()
    assert transport._stop_event.is_set()

@pytest.mark.asyncio
async def test_datastore_handlers_coverage(mock_bridge):
    service = mock_bridge
    await service._on_mcu_datastore_put(pb.DatastorePut(key="k", value=b"v"))
    assert service.state.datastore_cache["k"] == b"v"
    await service._on_mcu_datastore_get(pb.DatastoreGet(key="k"))
    assert service.serial.send.called
    service.serial.send.reset_mock()
    await service._on_mcu_datastore_get(pb.DatastoreGet(key="miss"))
    assert service.serial.send.called

def test_handshake_timing_coverage():
    config = RuntimeConfig(
        mqtt_topic="br", serial_port="/dev/ttytest", serial_baud=9600,
        mqtt_host="localhost", mqtt_port=1883,
        serial_retry_timeout=0.5, serial_response_timeout=1.0, serial_retry_attempts=3
    )
    timing = derive_serial_timing(config)
    assert timing.ack_timeout_ms == 500

@pytest.mark.asyncio
async def test_mailbox_handlers_coverage(mock_bridge):
    service = mock_bridge
    await service._on_mcu_mailbox_available(1)
    assert service.serial.send.called
    service.serial.send.reset_mock()
    await service._on_mcu_mailbox_read(1)
    assert service.serial.send.called

@pytest.mark.asyncio
async def test_handshake_manager_branches(mock_bridge):
    service = mock_bridge
    timing = derive_serial_timing(service.config)
    
    handshake = SerialHandshakeManager(
        config=service.config,
        state=service.state,
        serial_timing=timing,
        send_frame=service.serial.send,
        enqueue_mqtt=service.enqueue_mqtt,
        acknowledge_frame=AsyncMock()
    )
    
    service.state.link_handshake_nonce = None
    res = await handshake.handle_link_sync_resp(1, b"payload")
    assert res is False
    
    service.config.serial_handshake_min_interval = 10.0
    service.state.link_handshake_nonce = b"nonce"
    service.state.handshake_rate_until = time.monotonic() + 5.0
    res = await handshake.handle_link_sync_resp(1, b"payload")
    assert res is False
    
    service.state.handshake_rate_until = 0
    res = await handshake.handle_link_sync_resp(1, b"not protobuf")
    assert res is False
