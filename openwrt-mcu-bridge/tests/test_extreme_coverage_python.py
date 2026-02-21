import asyncio
import logging
from unittest.mock import AsyncMock, MagicMock, patch

import msgspec
import pytest
from mcubridge.config.logging import StructuredLogFormatter, configure_logging
from mcubridge.protocol.topics import Topic
from mcubridge.services.handshake import SerialHandshakeManager, SerialTimingWindow
from mcubridge.services.pin import PinComponent
from mcubridge.services.process import ProcessComponent
from mcubridge.state.context import ManagedProcess
from mcubridge.transport.serial import BridgeSerialProtocol
from mcubridge.util import mqtt_helper


@pytest.mark.asyncio
async def test_pin_component_extreme_gaps(runtime_config, runtime_state):
    """Cover missing lines in pin.py."""
    ctx = MagicMock()
    ctx.send_frame = AsyncMock(return_value=True)
    ctx.enqueue_mqtt = AsyncMock()
    ctx.publish = AsyncMock()
    ctx.is_command_allowed = MagicMock(return_value=True)
    comp = PinComponent(runtime_config, runtime_state, ctx)

    # Gap: handle_digital_read_resp with malformed payload
    await comp.handle_digital_read_resp(b"")  # Too short
    await comp.handle_digital_read_resp(b"\x01\x02\x03")  # Too long

    # Gap: handle_mqtt with invalid modes/actions
    await comp.handle_mqtt(Topic.DIGITAL, ["br", "d", "13", "mode"], "99")  # Invalid mode 99
    await comp.handle_mqtt(Topic.DIGITAL, ["br", "d", "13", "mode"], "invalid")  # Not an int

    # Gap: handle_mqtt unknown subtopic
    await comp.handle_mqtt(Topic.DIGITAL, ["br", "d", "13", "unknown"], "val")

    # Gap: handle_mqtt analog read with 'A' prefix parsing
    await comp.handle_mqtt(Topic.ANALOG, ["br", "a", "A0", "read"], "")
    await comp.handle_mqtt(Topic.ANALOG, ["br", "a", "a1", "read"], "")

    # Gap: _notify_pin_queue_overflow
    await comp._notify_pin_queue_overflow(Topic.DIGITAL, 13, None)


@pytest.mark.asyncio
async def test_mqtt_transport_extreme_gaps(runtime_config, runtime_state):
    """Cover missing lines in transport/mqtt.py (functional)."""

    # Create a local config copy to avoid patching issues
    local_config = msgspec.structs.replace(runtime_config, mqtt_tls=True)

    # Gap: _configure_tls with missing file
    with patch.object(local_config, "mqtt_cafile", "/non/existent/ca.pem"):
        with pytest.raises(RuntimeError, match="MQTT TLS CA file missing"):
            mqtt_helper.configure_tls_context(local_config)

    # Gap: _configure_tls with insecure mode
    with patch("ssl.create_default_context"):
        local_config = msgspec.structs.replace(runtime_config, mqtt_tls=True, mqtt_cafile=None)
        mqtt_helper.configure_tls_context(local_config)


@pytest.mark.asyncio
async def test_process_component_deep_gaps(runtime_config, runtime_state):
    """Cover missing lines in process.py (timeouts and tree cleanup)."""
    ctx = MagicMock()
    ctx.send_frame = AsyncMock(return_value=True)
    ctx.enqueue_mqtt = AsyncMock()
    ctx.schedule_background = AsyncMock()

    comp = ProcessComponent(runtime_config, runtime_state, ctx)

    # Gap: Process slot exhaustion
    runtime_config = msgspec.structs.replace(runtime_config, process_max_concurrent=1)
    comp = ProcessComponent(runtime_config, runtime_state, ctx)
    await comp._try_acquire_process_slot()
    assert await comp._try_acquire_process_slot() is False

    # Gap: handle_kill with ProcessLookupError
    mock_proc = MagicMock()
    mock_proc.wait = AsyncMock()
    mock_proc.returncode = 0
    slot = ManagedProcess(pid=1, command="test", handle=mock_proc)
    runtime_state.running_processes[1] = slot

    with patch("psutil.Process", side_effect=ProcessLookupError):
        await comp.handle_kill(b"\x00\x01")  # PID 1

    # Gap: _terminate_process_tree exceptions
    with patch("asyncio.to_thread") as mock_thread:
        mock_thread.side_effect = Exception("OS Error")
        mock_proc_tree = MagicMock()
        mock_proc_tree.returncode = None
        mock_proc_tree.pid = 999
        with pytest.raises(Exception, match="OS Error"):
            await comp._terminate_process_tree(mock_proc_tree)


@pytest.mark.asyncio
async def test_handshake_service_gaps(runtime_config, runtime_state):
    """Cover missing lines in handshake.py."""
    timing = SerialTimingWindow(ack_timeout_ms=200, response_timeout_ms=1000, retry_limit=3)
    handshake = SerialHandshakeManager(
        config=runtime_config,
        state=runtime_state,
        serial_timing=timing,
        send_frame=AsyncMock(return_value=True),
        enqueue_mqtt=AsyncMock(),
        acknowledge_frame=AsyncMock(),
    )

    # Gap: handle_sync_resp with invalid length
    await handshake.handle_link_sync_resp(b"\x00" * 5)  # Too short

    # Gap: handle_handshake_failure branches
    await handshake.handle_handshake_failure("test_reason", detail="test_detail")
    assert runtime_state.handshake_failures > 0


@pytest.mark.asyncio
async def test_serial_fast_extreme_gaps(runtime_config, runtime_state):
    """Cover missing lines in transport/serial_fast.py."""
    service = MagicMock()
    service.handle_mcu_frame = AsyncMock()
    loop = asyncio.get_running_loop()
    proto = BridgeSerialProtocol(service, runtime_state, loop)
    proto.connection_made(MagicMock())

    # Gap: data_received with massive garbage
    proto.data_received(b"\x00" * 2000)

    # Gap: _log_frame with exceptions
    frame = MagicMock()
    frame.command_id = 0x99  # Non-existent command
    with patch("mcubridge.transport.serial.logger.debug", side_effect=Exception("Log Error")):
        proto._log_frame(frame, "TX")


@pytest.mark.asyncio
async def test_config_logging_gaps(runtime_config):
    """Cover missing lines in config/logging.py."""

    # Gap: StructuredLogFormatter with bytes and complex objects
    formatter = StructuredLogFormatter()
    record = logging.LogRecord("test", logging.INFO, "path", 10, "msg", None, None)
    record.extra_data = {"bytes": b"\x01\x02", "obj": object()}
    output = formatter.format(record)
    assert "msg" in output

    # Gap: configure_logging with missing syslog
    with patch("logging.handlers.SysLogHandler", side_effect=ImportError):
        configure_logging(runtime_config)
