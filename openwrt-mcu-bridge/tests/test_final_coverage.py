"Final coverage tests to reach 100%."

from __future__ import annotations

import asyncio
import logging
import os
import ssl
from pathlib import Path
from unittest.mock import MagicMock, patch, AsyncMock
import struct
import tenacity
import pytest
import msgspec
from aiomqtt.message import Message

from mcubridge.config import logging as logging_config
from mcubridge.config import settings
from mcubridge.rpc import rle
from mcubridge.services.components.console import ConsoleComponent
from mcubridge.services.components.datastore import DatastoreComponent, DatastoreAction
from mcubridge.services.components.file import FileComponent, FileAction
from mcubridge.services.components.mailbox import MailboxComponent
from mcubridge.services.components.pin import PinComponent
from mcubridge.services.components.process import ProcessComponent
from mcubridge.rpc.protocol import Command, Status
from mcubridge.state.context import RuntimeState, McuCapabilities
from mcubridge.mqtt.messages import QueuedPublish
from mcubridge.protocol.topics import Topic


@pytest.fixture
def real_config():
    raw = settings.get_default_config()
    raw["serial_shared_secret"] = b"abcd1234"
    raw["serial_retry_timeout"] = 1.0
    raw["serial_response_timeout"] = 2.0
    raw["serial_handshake_fatal_failures"] = 15
    raw["process_max_concurrent"] = 4
    config = msgspec.convert(raw, settings.RuntimeConfig, strict=False)
    return config


def test_logging_config_candidates_branch():
    with patch("mcubridge.config.logging.SYSLOG_SOCKET", Path("/not/dev/log")):
        handler = logging_config._build_handler()
        assert isinstance(handler, (logging.Handler))


def test_settings_validation_errors_coverage():
    raw = settings.get_default_config()
    raw["serial_shared_secret"] = b"aaaaaaaa"
    with pytest.raises(msgspec.ValidationError, match="four distinct bytes"):
        msgspec.convert(raw, settings.RuntimeConfig, strict=False)

    raw = settings.get_default_config()
    raw["serial_shared_secret"] = b"abcd1234"
    raw["mailbox_queue_limit"] = 100
    raw["mailbox_queue_bytes_limit"] = 50
    with pytest.raises(msgspec.ValidationError, match="mailbox_queue_bytes_limit must be greater"):
        msgspec.convert(raw, settings.RuntimeConfig, strict=False)


def test_rle_decode_long_run_branch():
    data = b"AAAAA"
    encoded = rle.encode(data)
    decoded = rle.decode(encoded)
    assert decoded == data


@pytest.mark.asyncio
async def test_console_component_gaps(runtime_state: RuntimeState, real_config):
    ctx = MagicMock()
    comp = ConsoleComponent(real_config, runtime_state, ctx)
    with patch.object(comp, "_iter_console_chunks", return_value=[b"a", b""]):
        runtime_state.mcu_is_paused = True
        await comp.handle_mqtt_input(b"payload")
        assert any(b"a" == c for c in runtime_state.console_to_mcu_queue)
    runtime_state.mcu_is_paused = False
    with patch.object(comp, "_iter_console_chunks", return_value=[b""]):
        await comp.handle_mqtt_input(b"payload")


@pytest.mark.asyncio
async def test_console_component_async_gaps(runtime_state: RuntimeState, real_config):
    ctx = MagicMock()
    ctx.send_frame = AsyncMock(return_value=False)
    comp = ConsoleComponent(real_config, runtime_state, ctx)
    runtime_state.mcu_is_paused = False
    await comp.handle_mqtt_input(b"payload")
    assert runtime_state.console_to_mcu_queue
    runtime_state.console_to_mcu_queue.clear()
    runtime_state.enqueue_console_chunk(b"abc", logging.getLogger())
    with patch.object(comp, "_iter_console_chunks", return_value=[b""]):
        await comp.flush_queue()
    runtime_state.console_to_mcu_queue.clear()
    runtime_state.enqueue_console_chunk(b"abc", logging.getLogger())
    ctx.send_frame = AsyncMock(return_value=False)
    with patch.object(comp, "_iter_console_chunks", return_value=[b"a"]):
        await comp.flush_queue()
    assert comp._iter_console_chunks(b"") == []


@pytest.mark.asyncio
async def test_datastore_component_gaps(runtime_state: RuntimeState, real_config):
    ctx = MagicMock()
    ctx.send_frame = AsyncMock(return_value=True)
    ctx.enqueue_mqtt = AsyncMock()
    comp = DatastoreComponent(real_config, runtime_state, ctx)
    payload = bytes([1, 1, ord('k')])
    assert await comp.handle_put(payload) is False
    assert await comp.handle_get_request(b"") is False
    assert await comp.handle_get_request(bytes([5, ord('k')])) is False
    runtime_state.datastore["big"] = "a" * 300
    await comp.handle_get_request(bytes([3]) + b"big")
    await comp.handle_mqtt(DatastoreAction.GET, [], b"", "")
    await comp.handle_mqtt("UNKNOWN", ["key"], b"", "")
    await comp._handle_mqtt_put("k" * 300, "v", None)
    await comp._handle_mqtt_put("k", "v" * 300, None)


@pytest.mark.asyncio
async def test_file_component_gaps(runtime_state: RuntimeState, real_config):
    ctx = MagicMock()
    comp = FileComponent(real_config, runtime_state, ctx)
    assert await comp.handle_remove(b"") is False
    assert await comp.handle_remove(bytes([5, ord('f')])) is False
    with patch.object(comp, "_perform_file_operation", return_value=(False, None, "error")):
        await comp.handle_mqtt(FileAction.READ, ["file.txt"], b"")
    assert comp._normalise_filename("..") is None
    with patch("pathlib.Path.mkdir", side_effect=OSError("perm")):
        assert comp._get_base_dir() is None
    with patch("pathlib.Path.exists", return_value=True), \
         patch("pathlib.Path.is_dir", return_value=False):
        assert comp._get_base_dir() is None
    runtime_state.file_write_max_bytes = 10
    res = await comp._write_with_quota(Path("/tmp/f"), b"a" * 20)
    assert res[0] is False
    with patch("pathlib.Path.open", side_effect=OSError()):
        res = await comp._write_with_quota(Path("/tmp/f"), b"data")
        assert res[0] is False


@pytest.mark.asyncio
async def test_mailbox_component_gaps(runtime_state: RuntimeState, real_config):
    ctx = MagicMock()
    ctx.enqueue_mqtt = AsyncMock()
    ctx.send_frame = AsyncMock(return_value=True)

    comp = MailboxComponent(real_config, runtime_state, ctx)
    await comp.handle_processed(b"a")
    assert await comp.handle_push(b"a") is False

    runtime_state.mailbox_incoming_queue.append(b"dummy")
    with patch("mcubridge.state.context.RuntimeState.pop_mailbox_incoming", return_value=None):
        await comp._handle_mqtt_read()

    runtime_state.mailbox_incoming_queue.clear()
    with patch("mcubridge.state.context.RuntimeState.pop_mailbox_message", return_value=None):
        await comp._handle_mqtt_read()

    inbound = MagicMock(spec=Message)
    inbound.topic = "br/mailbox/write"
    with patch("msgspec.json.encode", return_value=b"{}"):
        await comp._handle_outgoing_overflow(10, inbound)


@pytest.mark.asyncio
async def test_pin_component_gaps(runtime_state: RuntimeState, real_config):
    ctx = MagicMock()
    ctx.send_frame = AsyncMock(return_value=True)
    ctx.enqueue_mqtt = AsyncMock()
    comp = PinComponent(real_config, runtime_state, ctx)
    await comp.handle_unexpected_mcu_request(Command.CMD_ANALOG_READ, b"")
    await comp.handle_analog_read_resp(b"a")
    await comp.handle_analog_read_resp(b"ab")
    await comp.handle_mqtt("d", ["br", "d"], "1")
    await comp.handle_mqtt("invalid", ["br", "d", "3", "read"], "1")
    await comp.handle_mqtt("d", ["br", "d", "invalid", "read"], "1")
    with patch.object(comp, "_validate_pin_access", return_value=False):
        await comp.handle_mqtt("d", ["br", "d", "3", "read"], "1")
    ctx.send_frame = AsyncMock(return_value=False)
    await comp._handle_read_command(Topic.DIGITAL, 3)
    await comp._handle_read_command(Topic.ANALOG, 3)
    assert comp._parse_pin_identifier("invalid") == -1
    assert comp._parse_pin_value(Topic.DIGITAL, "abc") is None
    runtime_state.mcu_capabilities = McuCapabilities(
        protocol_version=2, board_arch=1, num_digital_pins=10, num_analog_inputs=5, features=0
    )
    assert comp._validate_pin_access(20, False) is False


@pytest.mark.asyncio
async def test_metrics_gaps(runtime_state: RuntimeState):
    from mcubridge import metrics
    with patch("mcubridge.metrics._emit_bridge_snapshot", side_effect=TypeError()):
        try:
            await asyncio.wait_for(
                metrics._bridge_snapshot_loop(runtime_state, MagicMock(), flavor="summary", seconds=0.01),
                timeout=0.05,
            )
        except (asyncio.TimeoutError, TypeError):
            pass

    exporter = metrics.PrometheusExporter(runtime_state, "127.0.0.1", 0)
    with patch("asyncio.base_events.Server.serve_forever", side_effect=asyncio.CancelledError()):
        try:
            await exporter.run()
        except asyncio.CancelledError:
            pass

    # _handle_client error branches
    mock_writer = MagicMock(spec=asyncio.StreamWriter)
    mock_writer.write = MagicMock()
    mock_writer.close = MagicMock()
    mock_writer.wait_closed = AsyncMock()

    mock_reader = AsyncMock(spec=asyncio.StreamReader)
    mock_reader.readline.side_effect = [b"GET /metrics HTTP/1.1\r\n", b"\r\n"]

    with patch.object(exporter, "_render_metrics", side_effect=RuntimeError("render fail")):
        await exporter._handle_client(mock_reader, mock_writer)


@pytest.mark.asyncio
async def test_spool_gaps(tmp_path: Path):
    from mcubridge.mqtt.spool import MQTTPublishSpool
    spool = MQTTPublishSpool(str(tmp_path), limit=1)
    assert spool._disk_queue is not None
    spool._disk_queue.appendleft(b"raw_bytes")
    (tmp_path / "1.msg").write_bytes(b"data")
    spool._disk_queue.clear()
    with patch.object(spool._disk_queue, "close", side_effect=OSError()):
        spool.close()
    spool._use_disk = False
    spool.requeue(QueuedPublish("topic", b"payload"))
    with patch.object(spool, "_disk_queue", MagicMock()) as mock_disk:
        mock_disk.close.side_effect = OSError()
        spool._activate_fallback()
    spool.limit = 0
    spool._trim_locked()


@pytest.mark.asyncio
async def test_transport_mqtt_gaps(runtime_state: RuntimeState, tmp_path: Path):
    from mcubridge.transport import mqtt
    # Ensure config has real attributes, not just MagicMock
    mock_conf = MagicMock(spec=settings.RuntimeConfig)
    mock_conf.tls_enabled = True
    mock_conf.mqtt_tls = True
    mock_conf.mqtt_cafile = None
    mock_conf.mqtt_certfile = "cert"
    mock_conf.mqtt_keyfile = None
    with pytest.raises(RuntimeError, match="Both mqtt_certfile and mqtt_keyfile"):
        mqtt._configure_tls(mock_conf)

    cafile = tmp_path / "ca.crt"
    cafile.write_text("ca")
    mock_conf.mqtt_cafile = str(cafile)
    mock_conf.mqtt_keyfile = "key"
    # Mock Path instance specifically for the string value
    with patch("mcubridge.transport.mqtt.Path") as mock_path_cls:
        mock_path_cls.return_value.exists.return_value = True
        with patch("ssl.SSLContext.load_verify_locations", side_effect=ssl.SSLError()):
            with pytest.raises(RuntimeError):
                mqtt._configure_tls(mock_conf)


@pytest.mark.asyncio
async def test_transport_serial_fast_gaps(runtime_state: RuntimeState):
    from mcubridge.transport import serial_fast
    assert serial_fast._is_binary_packet(b"") is False
    retry_state = MagicMock()
    retry_state.attempt_number = 2
    serial_fast._log_baud_retry(retry_state)
    proto = serial_fast.BridgeSerialProtocol(MagicMock(), runtime_state, asyncio.get_running_loop())
    proto.connection_made(MagicMock())
    proto.connection_lost(Exception("lost"))
    proto._discarding = True
    proto.data_received(b"abc\x00")
    assert proto._discarding is False


@pytest.mark.asyncio
async def test_process_component_gaps(runtime_state: RuntimeState, real_config):
    ctx = MagicMock()
    ctx.send_frame = AsyncMock()
    comp = ProcessComponent(real_config, runtime_state, ctx)

    with patch("asyncio.create_subprocess_exec", side_effect=OSError()):
        await comp.handle_run(b"ls")

    with patch("os.kill", side_effect=ProcessLookupError()):
        await comp.handle_kill(msgspec.json.encode({"pid": 123}))

    mock_proc = AsyncMock()
    mock_proc.pid = 123
    with patch("asyncio.create_subprocess_exec", return_value=mock_proc), \
         patch("asyncio.TaskGroup.__aenter__", side_effect=OSError()):
        await comp.run_sync("ls")

    mock_task = MagicMock()
    mock_task.result.side_effect = RuntimeError()

    def _capture_and_close(coro):
        coro.close()
        return mock_task

    with patch("asyncio.TaskGroup.create_task", side_effect=_capture_and_close), \
         patch("asyncio.create_subprocess_exec", return_value=AsyncMock()):
        await comp.run_sync("ls")

    with patch("asyncio.create_subprocess_exec", side_effect=OSError()):
        await comp.start_async("ls")

    assert (await comp.collect_output(999)).status_byte == Status.ERROR.value

    mock_reader = AsyncMock()
    mock_reader.read.side_effect = OSError()
    await comp._consume_stream(123, mock_reader, bytearray())

    await comp._finalize_async_process(999, MagicMock())

    await comp._finalize_async_process(123, MagicMock())


@pytest.mark.asyncio
async def test_handshake_gaps(runtime_state: RuntimeState, real_config):
    from mcubridge.services import handshake
    from tenacity import RetryCallState

    retry_state = MagicMock(spec=RetryCallState)
    retry_state.attempt_number = 1
    retry_state.next_action = MagicMock()
    retry_state.next_action.sleep = 1.0
    handshake._log_handshake_retry(retry_state)

    mgr = handshake.SerialHandshakeManager(
        config=real_config,
        state=runtime_state,
        serial_timing=MagicMock(),
        send_frame=AsyncMock(return_value=True),
        enqueue_mqtt=AsyncMock(),
        acknowledge_frame=AsyncMock(),
        logger_=logging.getLogger("test")
    )

    with patch("tenacity.AsyncRetrying.__aiter__", side_effect=tenacity.RetryError(MagicMock())):
        assert await mgr.synchronize() is False

    with patch("mcubridge.services.handshake.validate_nonce_counter", return_value=(False, 0)):
        await mgr.handle_link_sync_resp(os.urandom(16) + b"a" * 16)

    mgr._send_frame = AsyncMock(return_value=False)
    assert await mgr._fetch_capabilities() is False

    mgr._parse_capabilities(b"short")
    with patch("struct.unpack", side_effect=struct.error()):
        mgr._parse_capabilities(b"a" * 8)

    runtime_state.handshake_backoff_until = 0
    assert mgr._handshake_backoff_remaining() == 0.0

    await mgr._publish_handshake_event("test", extra={"foo": "bar"})

    with patch("mcubridge.rpc.protocol.HANDSHAKE_CONFIG_FORMAT", ""):
        assert mgr._build_reset_payload() == b""


@pytest.mark.asyncio
async def test_runtime_gaps(runtime_state: RuntimeState, real_config):
    from mcubridge.services import runtime

    await runtime._background_task_runner(asyncio.sleep(0), task_name="test")

    svc = runtime.BridgeService(real_config, runtime_state)

    async with svc:
        pass

    svc.sync_link = AsyncMock(side_effect=OSError())
    await svc.on_serial_connected()

    svc.sync_link = AsyncMock(return_value=True)
    svc._system.request_mcu_version = AsyncMock(side_effect=OSError())
    svc._console.flush_queue = AsyncMock(side_effect=OSError())
    await svc.on_serial_connected()

    svc._dispatcher.dispatch_mcu_frame = AsyncMock(side_effect=ValueError())
    await svc.handle_mcu_frame(0x40, b"")

    svc._dispatcher.dispatch_mqtt_message = AsyncMock(side_effect=ValueError())
    await svc.handle_mqtt_message(MagicMock())

    svc._handshake.handle_capabilities_resp(b"")

    svc._serial_sender = None
    await svc._acknowledge_mcu_frame(0x40)
    svc._serial_sender = AsyncMock(side_effect=OSError())
    await svc._acknowledge_mcu_frame(0x40)

    await svc._handle_ack(b"")
    await svc.handle_status(Status.ERROR, b"error msg")
    await svc._process.handle_kill(b"")


def test_routers_overload():
    from mcubridge.services.routers import MCUHandlerRegistry
    reg = MCUHandlerRegistry()
    reg.register(0x40, AsyncMock())
    reg.register(0x40, AsyncMock())


def test_payload_validation_gaps():
    from mcubridge.services import payloads
    with pytest.raises(payloads.PayloadValidationError):
        payloads.ShellCommandPayload.from_mqtt(b"")

    with pytest.raises(payloads.PayloadValidationError):
        payloads.ShellPidPayload.from_topic_segment("abc")


def test_frame_debug_import_error():
    with pytest.raises(ImportError, match="developer-only tool"):
        import mcubridge.tools.frame_debug  # noqa: F401
