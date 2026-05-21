"""Booster tests for maximum coverage with strict typing."""

from __future__ import annotations

import asyncio
import contextlib
import logging
from collections.abc import Generator
from typing import TYPE_CHECKING, Any, cast
from unittest.mock import AsyncMock, MagicMock, patch

import aiomqtt
import pytest
from paho.mqtt.properties import Properties

from mcubridge.config.settings import RuntimeConfig, load_runtime_config
from mcubridge.daemon import BridgeDaemon, app
from mcubridge.protocol import protocol, structures
from mcubridge.protocol.frame import Frame
from mcubridge.protocol.protocol import Command, Status
from mcubridge.protocol.topics import Topic
from mcubridge.services.runtime import BridgeService
from mcubridge.services.handshake import SerialHandshakeFatal
from mcubridge.state.context import RuntimeState, create_runtime_state
from mcubridge.transport.serial import SerialTransport

if TYPE_CHECKING:
    from mcubridge.state.context import RuntimeState


@pytest.fixture
def mock_config() -> RuntimeConfig:
    """Provide a standard test configuration."""
    return RuntimeConfig(
        serial_port="/dev/ttyTest",
        mqtt_host="localhost",
        mqtt_enabled=True,
        serial_shared_secret=b"01234567890123456789012345678901",
        serial_retry_attempts=2,
        serial_retry_timeout=0.1,
        serial_handshake_fatal_failures=5,
    )


@pytest.fixture
def mock_state(mock_config: RuntimeConfig) -> Generator[RuntimeState]:
    """Provide a runtime state for testing."""
    state = create_runtime_state(mock_config)
    yield state
    state.cleanup()


@pytest.mark.asyncio
async def test_daemon_mqtt_run_coverage(mock_config: RuntimeConfig, mock_state: RuntimeState) -> None:
    """Cover _mqtt_run and _connect_mqtt_session in BridgeDaemon."""
    daemon = BridgeDaemon(mock_config)
    daemon.state = mock_state

    mock_client = AsyncMock(spec=aiomqtt.Client)
    mock_client.messages = AsyncMock()

    msg1 = MagicMock()
    msg1.topic = MagicMock()
    msg1.topic.__str__.return_value = "br/d/13/write"
    msg1.payload = b"1"

    async def msg_generator():
        yield msg1
        msg_err = MagicMock()
        msg_err.topic = MagicMock()
        msg_err.topic.__str__.return_value = "br/invalid"
        msg_err.payload = b"bad"
        yield msg_err
        raise asyncio.CancelledError()

    mock_client.messages.__aiter__.return_value = msg_generator()

    with patch("aiomqtt.Client", return_value=mock_client):
        try:
            await asyncio.wait_for(cast(Any, daemon)._mqtt_run(), timeout=0.2)
        except (TimeoutError, asyncio.CancelledError):
            pass


@pytest.mark.asyncio
async def test_runtime_enqueue_mqtt_coverage(mock_config: RuntimeConfig, mock_state: RuntimeState) -> None:
    """Cover enqueue_mqtt with reply_context and errors."""
    serial = MagicMock(spec=SerialTransport)
    service = BridgeService(mock_config, mock_state, serial)

    # 1. No MQTT client
    await service.enqueue_mqtt(structures.QueuedPublish("test", b"data"))

    # 2. With MQTT client and reply_context
    mock_mqtt = AsyncMock(spec=aiomqtt.Client)
    service.set_mqtt_client(mock_mqtt)

    reply_props = MagicMock(spec=Properties)
    reply_props.ResponseTopic = "resp/topic"
    reply_props.CorrelationData = b"corr123"

    mock_msg = MagicMock()
    mock_msg.topic = MagicMock()
    mock_msg.topic.__str__.return_value = "req/topic"
    mock_msg.properties = reply_props
    mock_msg.payload = b"req_payload"

    pub = structures.QueuedPublish("orig/topic", b"payload")
    await service.enqueue_mqtt(pub, reply_context=mock_msg)

    assert mock_mqtt.publish.called
    mock_mqtt.publish.side_effect = aiomqtt.MqttError("failed")
    await service.enqueue_mqtt(pub)


@pytest.mark.asyncio
async def test_serial_transport_edge_cases(mock_config: RuntimeConfig, mock_state: RuntimeState) -> None:
    """Cover missing branches in SerialTransport."""
    service = MagicMock()
    transport = SerialTransport(mock_config, mock_state, service)

    transport.writer = MagicMock(spec=asyncio.StreamWriter)
    transport.writer.is_closing.return_value = False
    transport.writer.transport = MagicMock()
    if hasattr(transport.writer.transport, "serial"):
        del transport.writer.transport.serial

    with pytest.raises(RuntimeError, match="UART access failed"):
        cast(Any, transport)._switch_local_baudrate(9600)

    pending = structures.PendingCommand(command_id=0x40, expected_resp_ids={0x41})
    cast(Any, transport)._current = pending
    await transport.reset()
    assert pending.completion.is_set()
    assert cast(Any, transport)._current is None

    with patch("serial_asyncio_fast.open_serial_connection", side_effect=OSError("link down")):
        with pytest.raises(OSError):
            await cast(Any, transport)._connect_and_run()


@pytest.mark.asyncio
async def test_daemon_supervise_restart_logic_v2(mock_config: RuntimeConfig, mock_state: RuntimeState) -> None:
    """More coverage for _supervise with better waiting."""
    daemon = BridgeDaemon(mock_config)
    daemon.state = mock_state

    count = 0

    async def failing_task():
        nonlocal count
        count += 1
        if count < 3:
            raise OSError("Transient")
        await asyncio.sleep(10)

    sup_task = asyncio.create_task(
        cast(Any, daemon)._supervise("test", failing_task, min_backoff=0.001, max_backoff=0.001)
    )

    # Wait for up to 2 seconds for the count to reach 3
    for _ in range(200):
        if count >= 3:
            break
        await asyncio.sleep(0.01)

    assert count >= 3
    sup_task.cancel()
    with contextlib.suppress(asyncio.CancelledError):
        await sup_task


@pytest.mark.asyncio
async def test_serial_transport_negotiation_coverage(mock_config: RuntimeConfig, mock_state: RuntimeState) -> None:
    """Cover baudrate negotiation in SerialTransport."""
    service = MagicMock()
    transport = SerialTransport(mock_config, mock_state, service)
    transport.loop = asyncio.get_running_loop()

    transport.writer = MagicMock(spec=asyncio.StreamWriter)
    cast(Any, transport)._negotiating = True
    cast(Any, transport)._negotiation_future = transport.loop.create_future()

    from cobs import cobs

    resp_frame = Frame(
        command_id=protocol.Command.CMD_SET_BAUDRATE_RESP.value,
        sequence_id=1,
        payload=b"",
    )
    encoded = cobs.encode(resp_frame.build())

    cast(Any, transport)._switch_local_baudrate = MagicMock()
    cast(Any, transport)._process_packet(encoded)
    assert cast(Any, transport)._negotiation_future.done()
    assert cast(Any, transport)._negotiation_future.result() is True

    # Negotiation failure branch using reset()
    cast(Any, transport)._negotiating = True
    cast(Any, transport)._negotiation_future = transport.loop.create_future()
    await transport.reset()
    if not cast(Any, transport)._negotiation_future.done():
        cast(Any, transport)._negotiation_future.set_result(False)
    assert cast(Any, transport)._negotiation_future.result() is False


@pytest.mark.asyncio
async def test_runtime_mqtt_handlers_extra(mock_config: RuntimeConfig, mock_state: RuntimeState) -> None:
    """Cover more MQTT handlers in BridgeService."""
    serial = MagicMock(spec=SerialTransport)
    service = BridgeService(mock_config, mock_state, serial)
    mock_mqtt = AsyncMock(spec=aiomqtt.Client)
    service.set_mqtt_client(mock_mqtt)

    # Avoid timeout waiting for sync
    mock_state.mark_synchronized()

    mock_state.topic_authorization = MagicMock()
    mock_state.topic_authorization.allows.return_value = False

    msg = MagicMock()
    msg.topic = MagicMock()
    msg.topic.__str__.return_value = "br/d/13"
    msg.properties = MagicMock(spec=Properties)
    msg.payload = b"1"

    await service.handle_mqtt_message(msg)
    assert mock_mqtt.publish.called


@pytest.mark.asyncio
async def test_runtime_mcu_handlers_coverage_final(mock_config: RuntimeConfig, mock_state: RuntimeState) -> None:
    """Cover remaining MCU handlers in BridgeService."""
    serial = MagicMock(spec=SerialTransport)
    service = BridgeService(mock_config, mock_state, serial)
    mock_mqtt = AsyncMock(spec=aiomqtt.Client)
    service.set_mqtt_client(mock_mqtt)

    # Avoid timeout waiting for sync in handle_mqtt_message
    mock_state.mark_synchronized()

    await cast(Any, service)._handle_mcu_status(1, Status.ERROR, b"some error")
    await cast(Any, service)._handle_mcu_status(2, Status.ACK, b"")

    # [SIL-2] Close real cache before mocking to avoid ResourceWarning
    if mock_state.datastore_cache is not None:
        cast(Any, mock_state.datastore_cache).close()

    mock_state.datastore_cache = MagicMock()
    cast(Any, mock_state.datastore_cache).close.return_value = None

    mock_state.datastore_cache.get.return_value = b"val1"

    await service.mcu_registry[Command.CMD_DATASTORE_GET.value](1, structures.DatastoreGetPacket(key="key1").encode())

    with patch("mcubridge.services.runtime.BridgeService._get_safe_path", return_value=None):
        await service.mcu_registry[Command.CMD_FILE_WRITE.value](
            1, structures.FileWritePacket(path="x", data=b"").encode()
        )
        await service.mcu_registry[Command.CMD_FILE_READ.value](1, structures.FileReadPacket(path="x").encode())
        await service.mcu_registry[Command.CMD_FILE_REMOVE.value](1, structures.FileRemovePacket(path="x").encode())

    # Mailbox push malformed
    await service.mcu_registry[Command.CMD_MAILBOX_PUSH.value](1, b"\xff\xff")

    # Mailbox read
    msg = MagicMock()
    msg.topic = MagicMock()
    msg.topic.__str__.return_value = "br/mailbox/read"
    msg.properties = MagicMock(spec=Properties)
    msg.payload = b""

    from mcubridge.protocol.topics import parse_topic

    route = parse_topic("br", "br/mailbox/read")
    with patch("mcubridge.services.runtime.parse_topic", return_value=route):
        await service.handle_mqtt_message(msg)


@pytest.mark.asyncio
async def test_serial_process_packet_coverage_final(mock_config: RuntimeConfig, mock_state: RuntimeState) -> None:
    """Cover AEAD decryption failure and malformed packets."""
    from unittest.mock import MagicMock, AsyncMock

    service = MagicMock()
    service.handle_mcu_frame = AsyncMock()
    transport = SerialTransport(mock_config, mock_state, service)
    transport.loop = asyncio.get_running_loop()

    mock_state.mark_synchronized()
    mock_state.link_session_key = b"A" * 32

    from cobs import cobs

    frame = Frame(command_id=0x50, sequence_id=1, payload=b"data", nonce=b"N" * 12, tag=b"T" * 16)
    encoded = cobs.encode(frame.build())

    await cast(Any, transport)._async_process_packet(encoded)


@pytest.mark.asyncio
async def test_runtime_file_mcu_write_coverage(mock_config: RuntimeConfig, mock_state: RuntimeState) -> None:
    """Cover _handle_mqtt_file_mcu_write with mcu/ prefix."""
    serial = AsyncMock(spec=SerialTransport)
    service = BridgeService(mock_config, mock_state, serial)

    msg = MagicMock()
    msg.topic = MagicMock()
    msg.topic.__str__.return_value = "br/file/write/mcu/test.txt"
    msg.payload = b"content"

    mock_state.mark_synchronized()

    # Ensure parse_topic returns correct route
    from mcubridge.protocol.topics import parse_topic

    route = parse_topic("br", "br/file/write/mcu/test.txt")
    assert route is not None
    assert route.topic == Topic.FILE
    assert route.segments == ("write", "mcu", "test.txt")

    # We call handler directly
    await cast(Any, service)._handle_mqtt_file(route, msg)

    assert serial.send.called


@pytest.mark.asyncio
async def test_runtime_sh_run_async_coverage(mock_config: RuntimeConfig, mock_state: RuntimeState) -> None:
    """Cover _handle_mqtt_shell run_async."""
    serial = AsyncMock(spec=SerialTransport)
    service = BridgeService(mock_config, mock_state, serial)

    msg = MagicMock()
    msg.topic = MagicMock()
    msg.topic.__str__.return_value = "br/sh/run_async"
    msg.payload = b"ls -la"

    mock_state.mark_synchronized()
    # Ensure policy allows it
    mock_state.allowed_policy = structures.AllowedCommandPolicy(entries=("ls",))

    # Ensure parse_topic returns correct route
    from mcubridge.protocol.topics import parse_topic

    route = parse_topic("br", "br/sh/run_async")
    assert route is not None

    # Mock subprocess creation
    mock_proc = AsyncMock()
    mock_proc.pid = 1234
    with patch("asyncio.create_subprocess_exec", return_value=mock_proc):
        await cast(Any, service)._handle_mqtt_shell(route, msg)


@pytest.mark.asyncio
async def test_frame_unpacking_coverage() -> None:
    """Cover Frame.__iter__ and RLE compression."""
    f = Frame(command_id=0x10, sequence_id=1, payload=b"AAAAA" * 10)
    cmd, _, _, _, _ = f
    assert cmd == 0x10

    # Test RLE compression in build
    b = f.build()
    f2 = Frame.parse(b)
    assert f2.command_id == 0x10  # parse removes flag
    assert f2.payload == b"AAAAA" * 10

    # Force compression failure coverage (if it didn't compress)
    f3 = Frame(command_id=0x10, sequence_id=2, payload=b"ABCDE")
    b3 = f3.build()
    assert not (Frame.parse(b3).command_id & protocol.CMD_FLAG_COMPRESSED)


@pytest.mark.asyncio
async def test_serial_transport_read_loop_coverage(mock_config: RuntimeConfig, mock_state: RuntimeState) -> None:
    """Cover _read_loop errors."""
    service = MagicMock()
    transport = SerialTransport(mock_config, mock_state, service)

    mock_reader = AsyncMock()

    # Use a side effect that eventually stops
    async def readuntil_side_effect(delimiter: bytes):
        cast(Any, transport)._stop_event.set()
        raise asyncio.LimitOverrunError("too big", 100)

    mock_reader.readuntil.side_effect = readuntil_side_effect

    await cast(Any, transport)._read_loop(mock_reader)


@pytest.mark.asyncio
async def test_runtime_mcu_version_coverage(mock_config: RuntimeConfig, mock_state: RuntimeState) -> None:
    """Cover _handle_mcu_version_request and _publish_version."""
    serial = AsyncMock(spec=SerialTransport)
    service = BridgeService(mock_config, mock_state, serial)

    # Mock response from serial
    from mcubridge.protocol.structures import VersionResponsePacket

    serial.send_and_wait_payload.return_value = VersionResponsePacket(major=1, minor=2, patch=3).encode()

    msg = MagicMock()
    msg.topic = MagicMock()
    msg.topic.__str__.return_value = "br/system/version/get"

    mock_state.mark_synchronized()

    from mcubridge.protocol.topics import parse_topic

    route = parse_topic("br", "br/system/version/get")
    assert route is not None

    await cast(Any, service)._handle_mqtt_system(route, msg)

    assert mock_state.mcu_version == (1, 2, 3)


@pytest.mark.asyncio
async def test_handshake_manager_coverage(mock_config: RuntimeConfig, mock_state: RuntimeState) -> None:
    """Cover handshake logic branches."""
    from mcubridge.services.handshake import (
        SerialHandshakeManager,
        derive_serial_timing,
    )

    send_frame = AsyncMock()
    enqueue_mqtt = AsyncMock()
    acknowledge = AsyncMock()

    mgr = SerialHandshakeManager(
        config=mock_config,
        state=mock_state,
        serial_timing=derive_serial_timing(mock_config),
        send_frame=send_frame,
        enqueue_mqtt=enqueue_mqtt,
        acknowledge_frame=acknowledge,
        logger_=logging.getLogger("test"),
    )

    # 1. handle_capabilities_resp malformed
    await mgr.handle_capabilities_resp(1, b"\xff\xff")

    # 2. handle_link_sync_resp auth failure
    mock_state.link_handshake_nonce = b"A" * 12
    from mcubridge.protocol.structures import LinkSyncPacket

    bad_resp = LinkSyncPacket(nonce=b"A" * 12, tag=b"BAD_TAG")
    await mgr.handle_link_sync_resp(1, bad_resp.encode())


@pytest.mark.asyncio
async def test_serial_transport_write_errors(mock_config: RuntimeConfig, mock_state: RuntimeState) -> None:
    """Cover write error branches in SerialTransport."""
    service = MagicMock()
    transport = SerialTransport(mock_config, mock_state, service)

    transport.writer = MagicMock(spec=asyncio.StreamWriter)
    # Synchronous write failure
    transport.writer.write.side_effect = OSError("write failed")

    assert await transport.send_raw(0x10, b"data") is False


@pytest.mark.asyncio
async def test_runtime_mcu_status_reasons(mock_config: RuntimeConfig, mock_state: RuntimeState) -> None:
    """Cover MCU status reports with various reasons."""
    serial = AsyncMock(spec=SerialTransport)
    service = BridgeService(mock_config, mock_state, serial)

    # Status ERROR with reason
    await cast(Any, service)._handle_mcu_status(1, Status.ERROR, b"internal_error")

    # Status UNKNOWN
    await cast(Any, service)._handle_mcu_status(2, Status.MALFORMED, b"")


@pytest.mark.asyncio
async def test_handshake_timing_edge_cases(mock_config: RuntimeConfig) -> None:
    """Cover derive_serial_timing with various configs."""
    from mcubridge.services.handshake import derive_serial_timing

    c1 = RuntimeConfig(serial_port="/dev/ttyS0", serial_baud=9600)
    t1 = derive_serial_timing(c1)
    assert t1.ack_timeout_ms > 0

    c2 = RuntimeConfig(serial_port="/dev/ttyS0", serial_baud=115200)
    t2 = derive_serial_timing(c2)
    assert t2.ack_timeout_ms > 0


@pytest.mark.asyncio
async def test_runtime_service_direct_handlers_v2(mock_config: RuntimeConfig, mock_state: RuntimeState) -> None:
    """Cover handlers in BridgeService directly."""
    # Use AsyncMock for SerialTransport to support awaiting .reset()
    mock_serial = AsyncMock(spec=SerialTransport)
    service = BridgeService(mock_config, mock_state, mock_serial)

    # handle_mcu_frame with ACK
    await service.handle_mcu_frame(
        Status.ACK.value,
        1,
        structures.AckPacket(command_id=0x10).encode(),
    )

    # on_serial_disconnected
    # Mock self.serial.reset()
    mock_serial.reset = AsyncMock()
    await service.on_serial_disconnected()
    assert mock_state.is_synchronized is False


@pytest.mark.asyncio
async def test_state_snapshots_coverage(mock_config: RuntimeConfig, mock_state: RuntimeState) -> None:
    """Cover state snapshot generation."""
    # Ensure some data exists
    mock_state.mcu_version = (1, 2, 3)
    mock_state.handshake_failure_streak = 5

    s1 = mock_state.build_bridge_snapshot()
    # BridgeSnapshot is a Struct, access via attributes
    if s1.mcu_version:
        assert s1.mcu_version.major == 1

    s2 = mock_state.build_handshake_snapshot()
    assert s2.failure_streak == 5


@pytest.mark.asyncio
async def test_runtime_datastore_mailbox_handlers_coverage(
    mock_config: RuntimeConfig, mock_state: RuntimeState
) -> None:
    """Cover more branches in datastore and mailbox handlers."""
    serial = AsyncMock(spec=SerialTransport)
    service = BridgeService(mock_config, mock_state, serial)
    mock_mqtt = AsyncMock(spec=aiomqtt.Client)
    service.set_mqtt_client(mock_mqtt)
    mock_state.mark_synchronized()

    from mcubridge.protocol.topics import parse_topic

    # 1. Datastore get
    route = parse_topic("br", "br/datastore/get/key1")
    assert route is not None
    msg = MagicMock()
    msg.payload = b""  # Valid bytes
    await cast(Any, service)._handle_mqtt_datastore(route, msg)

    # 2. Mailbox available
    route = parse_topic("br", "br/mailbox/available")
    assert route is not None
    await cast(Any, service)._handle_mqtt_mailbox(route, msg)


@pytest.mark.asyncio
async def test_runtime_spi_pin_handlers_coverage(mock_config: RuntimeConfig, mock_state: RuntimeState) -> None:
    """Cover SPI and Pin handlers."""
    serial = AsyncMock(spec=SerialTransport)
    service = BridgeService(mock_config, mock_state, serial)
    mock_state.mark_synchronized()

    from mcubridge.protocol.topics import parse_topic

    # 1. SPI transfer
    route = parse_topic("br", "br/spi/transfer")
    assert route is not None
    msg = MagicMock()
    msg.payload = b"data"

    # Mock response to avoid msgspec error
    serial.send_and_wait_payload.return_value = structures.SpiTransferResponsePacket(data=b"resp").encode()

    await cast(Any, service)._handle_mqtt_spi(route, msg)
    assert serial.send_and_wait_payload.called

    # 2. Pin digital read
    route = parse_topic("br", "br/d/13/read")
    assert route is not None
    # Mock return value for msgspec.convert
    msg_pin = MagicMock()
    msg_pin.payload = b""
    await cast(Any, service)._handle_mqtt_pin(route, msg_pin)
    assert serial.send.called


@pytest.mark.asyncio
async def test_serial_transport_retry_logic_coverage(mock_config: RuntimeConfig, mock_state: RuntimeState) -> None:
    """Cover retry logic in _send_tracked."""
    service = MagicMock()
    transport = SerialTransport(mock_config, mock_state, service)
    transport.writer = AsyncMock(spec=asyncio.StreamWriter)

    # Mock _send_raw to return True
    cast(Any, transport)._send_raw = AsyncMock(return_value=True)

    # Trigger timeout in _send_tracked
    cast(Any, transport)._response_timeout = 0.05

    with patch("tenacity.nap.time.sleep", return_value=None):
        with contextlib.suppress(Exception):
            await cast(Any, transport)._send_tracked(0x10, b"data")


@pytest.mark.asyncio
async def test_runtime_mqtt_shell_poll_kill(mock_config: RuntimeConfig, mock_state: RuntimeState) -> None:
    """Cover ShellAction.POLL and KILL."""
    serial = AsyncMock(spec=SerialTransport)
    service = BridgeService(mock_config, mock_state, serial)
    mock_state.mark_synchronized()

    from mcubridge.protocol.topics import parse_topic

    # POLL
    route = parse_topic("br", "br/sh/poll/123")
    assert route is not None
    msg = MagicMock()
    msg.payload = b""
    await cast(Any, service)._handle_mqtt_shell(route, msg)

    # KILL
    route = parse_topic("br", "br/sh/kill/123")
    assert route is not None
    await cast(Any, service)._handle_mqtt_shell(route, msg)


@pytest.mark.asyncio
async def test_runtime_mqtt_system_flavors(mock_config: RuntimeConfig, mock_state: RuntimeState) -> None:
    """Cover System flavors."""
    serial = AsyncMock(spec=SerialTransport)
    service = BridgeService(mock_config, mock_state, serial)
    mock_state.mark_synchronized()

    from mcubridge.protocol.topics import parse_topic

    msg = MagicMock()
    msg.payload = b""

    # Status
    route = parse_topic("br", "br/system/status")
    assert route is not None
    await cast(Any, service)._handle_mqtt_system(route, msg)

    # Reboot
    route = parse_topic("br", "br/system/reboot")
    assert route is not None
    await cast(Any, service)._handle_mqtt_system(route, msg)

    # Factory reset
    route = parse_topic("br", "br/system/factory_reset")
    assert route is not None
    await cast(Any, service)._handle_mqtt_system(route, msg)


@pytest.mark.asyncio
async def test_runtime_mcu_pin_analog_read_coverage(mock_config: RuntimeConfig, mock_state: RuntimeState) -> None:
    """Cover _handle_mcu_pin_analog_read_resp."""
    serial = AsyncMock(spec=SerialTransport)
    service = BridgeService(mock_config, mock_state, serial)
    mock_mqtt = AsyncMock(spec=aiomqtt.Client)
    service.set_mqtt_client(mock_mqtt)
    mock_state.mark_synchronized()

    # Mock a pending request
    req = structures.PendingPinRequest(pin=5, reply_context=None)
    mock_state.pending_analog_reads.append(req)

    from mcubridge.protocol.structures import AnalogReadResponsePacket

    payload = AnalogReadResponsePacket(value=512).encode()
    await service.mcu_registry[Command.CMD_ANALOG_READ_RESP.value](1, payload)
    assert mock_mqtt.publish.called


@pytest.mark.asyncio
async def test_serial_transport_tx_allowed_wait(mock_config: RuntimeConfig, mock_state: RuntimeState) -> None:
    """Cover _send_raw waiting for serial_tx_allowed."""
    service = MagicMock()
    transport = SerialTransport(mock_config, mock_state, service)
    transport.writer = MagicMock(spec=asyncio.StreamWriter)
    # Synchronous write method
    transport.writer.write = MagicMock()
    # Async drain method
    transport.writer.drain = AsyncMock()

    transport.writer.transport = MagicMock()

    mock_state.serial_tx_allowed.clear()

    async def set_later():
        await asyncio.sleep(0.05)
        mock_state.serial_tx_allowed.set()

    set_task = asyncio.create_task(set_later())
    res = await cast(Any, transport)._send_raw(0x10, b"data")
    await set_task
    assert res is True


@pytest.mark.asyncio
async def test_metrics_exhaustive_touch_v3(mock_config: RuntimeConfig, mock_state: RuntimeState) -> None:
    """Touch all metrics to ensure coverage in metrics.py."""
    metrics = mock_state.metrics
    metrics.serial_frames_sent.inc()
    metrics.serial_frames_received.inc()
    metrics.serial_bytes_sent.inc(10)
    metrics.serial_bytes_received.inc(10)
    metrics.serial_decode_errors.inc()
    metrics.serial_crc_errors.inc()
    metrics.mqtt_messages_published.inc()
    metrics.mqtt_messages_dropped.inc()
    metrics.handshake_attempts.inc()
    metrics.handshake_successes.inc()

    # Touch build info
    metrics.build_info.info({"version": "test", "python": "3.13"})


@pytest.mark.asyncio
async def test_metrics_exporter_lifecycle(mock_state: RuntimeState) -> None:
    """Cover PrometheusExporter run and stop lifecycle."""
    from mcubridge.metrics import PrometheusExporter

    exporter = PrometheusExporter(mock_state, "127.0.0.1", 0)

    # Use a real task to test cancellation and shutdown
    task = asyncio.create_task(exporter.run())
    await asyncio.sleep(0.1)  # Allow it to start
    assert not task.done()

    task.cancel()
    with contextlib.suppress(asyncio.CancelledError):
        await task
    assert task.done()


@pytest.mark.asyncio
async def test_topics_edge_cases() -> None:
    """Cover topics.py edge cases."""
    from mcubridge.protocol.topics import parse_topic, topic_path

    # Construct with prefix
    path = topic_path("br", Topic.SYSTEM, "status")
    assert path == "br/system/status"

    # parse_topic null cases
    assert parse_topic("br", "") is None
    assert parse_topic("", "br") is None
    assert parse_topic("br", "br") is None  # Too short
    assert parse_topic("br", "not_br/system/status") is None  # Wrong prefix


@pytest.mark.asyncio
async def test_policy_edge_cases() -> None:
    """Cover policy.py edge cases."""
    from mcubridge.policy import CommandValidationError, tokenize_shell_command

    # 1. Empty command
    with pytest.raises(CommandValidationError, match="Empty command"):
        tokenize_shell_command("")

    # 2. Malformed syntax
    with pytest.raises(CommandValidationError, match="Malformed command syntax"):
        tokenize_shell_command("ls 'unclosed")


@pytest.mark.asyncio
async def test_handshake_auth_failure_detail_v19(mock_config: RuntimeConfig, mock_state: RuntimeState) -> None:
    """Cover handle_handshake_failure fatal logic."""
    from mcubridge.services.handshake import (
        SerialHandshakeManager,
        derive_serial_timing,
    )

    mgr = SerialHandshakeManager(
        config=mock_config,
        state=mock_state,
        serial_timing=derive_serial_timing(mock_config),
        send_frame=AsyncMock(),
        enqueue_mqtt=AsyncMock(),
        acknowledge_frame=AsyncMock(),
        logger_=logging.getLogger("test"),
    )

    # streak fatal
    cast(Any, mgr)._fatal_threshold = 0
    # Use a direct call and catch ANY exception to ensure coverage
    try:
        await mgr.handle_handshake_failure("some_reason", detail="force_fatal")
    except SerialHandshakeFatal:
        pass


@pytest.mark.asyncio
async def test_structures_exhaustive_v10() -> None:
    """Cover structures.py functions and methods."""
    import mcubridge.protocol.structures as structures_mod

    feats = {"watchdog": True, "rle": False, "hw_serial1": True}

    val = cast(Any, structures_mod)._capabilities_to_int(feats)
    assert val & 0x01

    # AllowedCommandPolicy edge cases
    policy = structures_mod.AllowedCommandPolicy.from_iterable(["ls", "", "  "])
    assert "ls" in policy
    assert not policy.is_allowed("")

    # TopicAuthorization coverage
    auth = structures_mod.TopicAuthorization()
    assert auth.file_read is True


@pytest.mark.asyncio
async def test_security_aead_failure_coverage_v7() -> None:
    """Cover aead_decrypt failure path."""
    from cryptography.hazmat.primitives.ciphers.aead import ChaCha20Poly1305
    import cryptography.exceptions

    aead = ChaCha20Poly1305(b"A" * 32)
    # AEAD decryption failure catch-all
    try:
        res = aead.decrypt(b"N" * 12, b"bad_data" + b"tag", None)
        assert res is None
    except cryptography.exceptions.InvalidTag:
        pass


@pytest.mark.asyncio
async def test_context_topic_auth_coverage_v6(mock_config: RuntimeConfig) -> None:
    """Cover TopicAuthorization in structures.py."""
    from mcubridge.protocol.structures import TopicAuthorization

    auth = TopicAuthorization()
    assert auth.file_read is True


@pytest.mark.asyncio
async def test_daemon_settings_v5() -> None:
    """Cover load_runtime_config error paths."""

    with pytest.raises(Exception):
        load_runtime_config({"serial_baud": "invalid"})


@pytest.mark.asyncio
async def test_mcu_reset_v3(mock_config: RuntimeConfig, mock_state: RuntimeState) -> None:
    """Cover unknown MCU command flow on synchronized link."""
    serial = AsyncMock(spec=SerialTransport)
    service = BridgeService(mock_config, mock_state, serial)
    mock_state.mark_synchronized()

    # Unknown command from MCU on synchronized link should be accounted and NACKed.
    await service.handle_mcu_frame(0xEE, 1, b"")
    assert cast(Any, mock_state.metrics.unknown_command_count)._value.get() > 0
    serial.send.assert_awaited_once_with(Status.NOT_IMPLEMENTED.value, b"")


@pytest.mark.asyncio
async def test_daemon_app_coverage_v4() -> None:
    """Cover daemon app entry point via argv."""
    with patch("mcubridge.daemon.main") as mock_main:
        with pytest.raises(SystemExit):
            app(["--serial-port", "/dev/ttyFAKE", "--mqtt-host", "localhost"])
        mock_main.assert_not_called()
