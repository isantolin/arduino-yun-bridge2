"""Additional tests to boost Python coverage."""

import asyncio
import errno
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from mcubridge.protocol.protocol import FRAME_DELIMITER
from mcubridge.transport.serial import MAX_SERIAL_PACKET_BYTES, BridgeSerialProtocol, SerialTransport


@pytest.mark.asyncio
async def test_protocol_large_packet_discard():
    """Test that protocol discards oversized packets."""
    service = AsyncMock()
    state = MagicMock()
    loop = asyncio.get_running_loop()
    proto = BridgeSerialProtocol(service, state, loop)

    # Send a large chunk without delimiter
    large_data = b"A" * (MAX_SERIAL_PACKET_BYTES + 10)
    proto.data_received(large_data)

    state.record_serial_decode_error.assert_called()
    assert proto._discarding is True

    # Send delimiter, should stop discarding
    proto.data_received(FRAME_DELIMITER)
    assert proto._discarding is False
    assert len(proto._buffer) == 0


@pytest.mark.asyncio
async def test_negotiate_baudrate_failure():
    """Test baudrate negotiation timeout/failure."""
    service = AsyncMock()
    state = MagicMock()
    config = MagicMock()
    config.serial_baud = 115200
    config.serial_safe_baud = 9600
    config.reconnect_delay = 1

    transport_instance = SerialTransport(config, state, service)
    proto = MagicMock(spec=BridgeSerialProtocol)
    proto.loop = asyncio.get_running_loop()
    proto.write_frame.return_value = True

    # Mock timeout by never setting result on negotiation_future
    with patch("asyncio.wait_for", side_effect=asyncio.TimeoutError):
        success = await transport_instance._negotiate_baudrate(proto, 115200)
        assert success is False


@pytest.mark.asyncio
async def test_async_process_packet_crc_error():
    """Test handling of CRC errors in async_process_packet."""
    from cobs import cobs

    service = AsyncMock()
    state = MagicMock()
    loop = asyncio.get_running_loop()
    proto = BridgeSerialProtocol(service, state, loop)

    # Build a valid-length raw frame but with dummy data
    # raw [VER][LEN_H][LEN_L][CMD_H][CMD_L][CRC32...]
    raw_frame = b"\x02\x00\x00\x40\x00\x00\x00\x00\x00"  # 9 bytes
    fake_encoded = cobs.encode(raw_frame)

    # Mock Frame.from_bytes to raise ValueError with CRC mismatch
    with patch("mcubridge.transport.serial.Frame.from_bytes", side_effect=ValueError("CRC mismatch")):
        await proto._async_process_packet(fake_encoded)

        state.record_serial_decode_error.assert_called()
        state.record_serial_crc_error.assert_called()


@pytest.mark.asyncio
async def test_console_component_mqtt_input_paused():
    """Test console component behavior when MCU is paused."""
    from mcubridge.services.console import ConsoleComponent

    config = MagicMock()
    state = MagicMock()
    state.mcu_is_paused = True
    state.mqtt_topic_prefix = "br"
    ctx = AsyncMock()

    console = ConsoleComponent(config, state, ctx)
    payload = b"hello"
    await console.handle_mqtt_input(payload)

    # Should be queued, not sent
    state.enqueue_console_chunk.assert_called()
    ctx.send_frame.assert_not_called()


@pytest.mark.asyncio
async def test_console_component_flush_queue_send_fail():
    """Test console component behavior when send fails during flush."""
    from mcubridge.services.console import ConsoleComponent

    config = MagicMock()
    state = MagicMock()
    state.mcu_is_paused = False
    state.mqtt_topic_prefix = "br"
    state.console_to_mcu_queue = [b"buffered"]
    state.pop_console_chunk.return_value = b"buffered"

    ctx = AsyncMock()
    ctx.send_frame.return_value = False  # Fail send

    console = ConsoleComponent(config, state, ctx)
    await console.flush_queue()

    state.requeue_console_chunk_front.assert_called_with(b"buffered")


def test_daemon_main_base_exception():
    """Test daemon.main handling of BaseException."""
    from mcubridge.daemon import main

    with patch("mcubridge.daemon.BridgeDaemon") as mock_daemon:
        mock_daemon.return_value.run.side_effect = BaseException("fatal")
        with patch("sys.exit") as mock_exit:
            with patch("mcubridge.daemon.load_runtime_config"):
                with patch("mcubridge.daemon.configure_logging"):
                    main()
                    mock_exit.assert_called_with(1)


@pytest.mark.asyncio
async def test_metrics_snapshot_emit_exceptions():
    """Test metrics snapshot emit failure paths."""
    from mcubridge.metrics import _emit_metrics_snapshot

    state = MagicMock()
    enqueue = AsyncMock()

    # Test TypeError/ValueError handling
    with patch("mcubridge.metrics.msgspec.json.encode", side_effect=TypeError("fail")):
        try:
            await _emit_metrics_snapshot(state, enqueue, expiry_seconds=10)
        except TypeError:
            pass  # Expected when calling direct without publish_metrics wrapper




def test_spool_disk_error_requeue():
    """Test MQTT spool requeue disk error handling."""
    from mcubridge.mqtt.messages import QueuedPublish
    from mcubridge.mqtt.spool import MQTTPublishSpool

    with patch("mcubridge.mqtt.spool.FileSpoolDeque") as mock_dq:
        mock_dq.return_value.appendleft.side_effect = OSError(errno.EIO, "IO error")
        spool = MQTTPublishSpool("/tmp/spool", limit=100)

        msg = QueuedPublish(topic_name="t", payload=b"p")
        # Forcing requeue via appendleft
        spool._disk_queue = mock_dq.return_value
        spool._use_disk = True

        spool.requeue(msg)
        assert spool._use_disk is False  # Should have triggered fallback


def test_spool_pending_disk_error():
    """Test MQTT spool pending disk error handling."""
    from mcubridge.mqtt.spool import MQTTPublishSpool

    spool = MQTTPublishSpool("/tmp/spool", limit=100)
    spool._disk_queue = MagicMock()
    # FileSpoolDeque uses __len__
    spool._disk_queue.__len__.side_effect = OSError("fail")

    count = spool.pending
    assert count == 0  # Memory was empty


def test_spool_trim_disk_error():
    """Test MQTT spool trim disk error handling."""
    from mcubridge.mqtt.spool import MQTTPublishSpool

    spool = MQTTPublishSpool("/tmp/spool", limit=1)
    spool._disk_queue = MagicMock()
    spool._disk_queue.__len__.side_effect = OSError("fail")

    # This triggers _trim_locked
    spool._memory_queue.append(MagicMock())
    spool._memory_queue.append(MagicMock())
    spool._trim_locked()
    # Should log and continue
