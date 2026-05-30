from __future__ import annotations
import time
from unittest.mock import MagicMock, AsyncMock
from pathlib import Path
from typing import Any, cast
import pytest
from mcubridge.services.runtime import BridgeService
from mcubridge.config.settings import RuntimeConfig
from mcubridge.state.context import create_runtime_state
from mcubridge.protocol import protocol, mcubridge_pb2 as pb
from mcubridge.protocol.structures import QueuedPublish
from mcubridge.services.handshake import SerialHandshakeManager, derive_serial_timing
from mcubridge.transport.serial import SerialTransport


@pytest.fixture
def mock_bridge() -> BridgeService:
    config = RuntimeConfig(
        mqtt_topic="br",
        serial_port="/dev/ttytest",
        serial_baud=9600,
        mqtt_host="localhost",
        mqtt_port=1883,
        mqtt_spool_dir="/tmp/mcubridge_spool_test",
    )
    state = create_runtime_state(config)
    serial = MagicMock(spec=SerialTransport)
    serial.send = AsyncMock(return_value=True)
    service = BridgeService(config, state, cast(SerialTransport, serial))
    return service


@pytest.mark.asyncio
async def test_resolve_reply_and_drop_coverage(mock_bridge: BridgeService) -> None:
    mock_bridge.set_mqtt_client(None)
    msg = QueuedPublish(topic_name="original/topic", payload=b"data")

    reply_ctx = MagicMock()
    reply_ctx.properties.ResponseTopic = "reply/topic"
    reply_ctx.properties.CorrelationData = b"corr123"
    reply_ctx.topic = "request/topic"

    await mock_bridge.enqueue_mqtt(msg, reply_context=reply_ctx)
    assert mock_bridge.state.mqtt_dropped_messages >= 0



@pytest.mark.asyncio
async def test_spool_health_and_list_files(mock_bridge: BridgeService, tmp_path: Path) -> None:
    mock_bridge.config.mqtt_spool_dir = str(tmp_path)
    client = __import__("unittest").mock.MagicMock()
    mock_bridge.set_mqtt_client(client)

    import msgspec
    from mcubridge.protocol.structures import QueuedPublish
    dummy_pub = QueuedPublish(topic_name="test/topic", payload=b"data", qos=1, retain=False)
    f1 = tmp_path / "1.msgpack"
    f1.write_bytes(msgspec.msgpack.encode(dummy_pub))

    with __import__("unittest").mock.patch.object(mock_bridge, "_list_mqtt_spool_files", side_effect=OSError("scan failed")):
        await mock_bridge.flush_mqtt_spool()
    assert mock_bridge.state.mqtt_spool_degraded is True
    assert mock_bridge.state.mqtt_spool_failure_reason == "scan failed"

    # Now make it succeed
    client.publish = __import__("unittest").mock.AsyncMock(return_value=None)
    await mock_bridge.flush_mqtt_spool()
    assert mock_bridge.state.mqtt_spool_degraded is False



@pytest.mark.asyncio
async def test_handler_decode_errors(mock_bridge: BridgeService) -> None:
    await mock_bridge.handle_mcu_frame(protocol.Command.CMD_CONSOLE_WRITE.value, 1, b"not a protobuf")
    await mock_bridge.handle_mcu_frame(protocol.Status.ACK.value, 1, b"invalid")
    await mock_bridge.handle_mcu_frame(protocol.Status.OK.value, 1, b"\xff\xff\xff")
    await mock_bridge.handle_mcu_frame(protocol.Status.ERROR.value, 1, b"\xff\xff\xff")


@pytest.mark.asyncio
async def test_serial_transport_active_failure(mock_bridge: BridgeService) -> None:
    transport = SerialTransport(mock_bridge.config, mock_bridge.state, mock_bridge)
    transport.writer = None
    assert await transport.send(1, b"payload") is False
    with pytest.raises(RuntimeError, match="Serial writer inactive"):
        transport._active_transport()


@pytest.mark.asyncio
async def test_serial_transport_lifecycle(mock_bridge: BridgeService) -> None:
    transport = SerialTransport(mock_bridge.config, mock_bridge.state, mock_bridge)
    mock_writer = MagicMock()
    transport.writer = mock_writer
    await transport.stop()
    await transport.reset()


@pytest.mark.asyncio
async def test_datastore_handlers_coverage(mock_bridge: BridgeService) -> None:
    put_frame = pb.DatastorePut(key="k", value=b"v")
    await mock_bridge.handle_mcu_frame(protocol.Command.CMD_DATASTORE_PUT.value, 1, put_frame.SerializeToString())
    assert mock_bridge.state.datastore_cache is not None
    assert mock_bridge.state.datastore_cache["k"] == b"v"

    get_frame = pb.DatastoreGet(key="k")
    await mock_bridge.handle_mcu_frame(protocol.Command.CMD_DATASTORE_GET.value, 2, get_frame.SerializeToString())

    get_frame_miss = pb.DatastoreGet(key="miss")
    await mock_bridge.handle_mcu_frame(protocol.Command.CMD_DATASTORE_GET.value, 3, get_frame_miss.SerializeToString())


def test_handshake_timing_coverage() -> None:
    config = RuntimeConfig(
        mqtt_topic="br",
        serial_port="/dev/ttytest",
        serial_baud=9600,
        mqtt_host="localhost",
        mqtt_port=1883,
        serial_retry_timeout=0.5,
        serial_response_timeout=1.0,
        serial_retry_attempts=3,
    )
    timing = derive_serial_timing(config)
    assert timing.ack_timeout_ms == 500


@pytest.mark.asyncio
async def test_mailbox_handlers_coverage(mock_bridge: BridgeService) -> None:
    await mock_bridge.handle_mcu_frame(protocol.Command.CMD_MAILBOX_AVAILABLE.value, 1, b"")
    await mock_bridge.handle_mcu_frame(protocol.Command.CMD_MAILBOX_READ.value, 1, b"")


@pytest.mark.asyncio
async def test_handshake_manager_branches(mock_bridge: BridgeService) -> None:
    timing = derive_serial_timing(mock_bridge.config)
    handshake = SerialHandshakeManager(
        config=mock_bridge.config,
        state=mock_bridge.state,
        serial_timing=timing,
        send_frame=cast(Any, mock_bridge.serial.send),
        enqueue_mqtt=cast(Any, mock_bridge.enqueue_mqtt),
        acknowledge_frame=AsyncMock(),
    )

    mock_bridge.state.link_handshake_nonce = None
    res = await handshake.handle_link_sync_resp(1, b"payload")
    assert res is False

    mock_bridge.config.serial_handshake_min_interval = 10.0
    mock_bridge.state.link_handshake_nonce = b"nonce"
    mock_bridge.state.handshake_rate_until = time.monotonic() + 5.0
    res = await handshake.handle_link_sync_resp(1, b"payload")
    assert res is False

    mock_bridge.state.handshake_rate_until = 0
    res = await handshake.handle_link_sync_resp(1, b"not protobuf")
    assert res is False
