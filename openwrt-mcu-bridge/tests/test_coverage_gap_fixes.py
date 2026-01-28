"""Tests to close coverage gaps in the mcubridge ecosystem."""

import logging
import asyncio
import struct
import time
import sys
from pathlib import Path
from unittest.mock import MagicMock, patch, AsyncMock
from types import SimpleNamespace
import pytest
import mcubridge.common as common
import mcubridge.metrics as metrics
import mcubridge.daemon as daemon
from mcubridge.services.handshake import SerialHandshakeManager, derive_serial_timing


# --- Helpers ---

def create_fake_config():
    """Create a real namespace config object to avoid MagicMock comparison issues."""
    return SimpleNamespace(
        serial_port="/dev/ttyFake",
        serial_baud=115200,
        serial_safe_baud=9600,
        mqtt_host="localhost",
        mqtt_port=1883,
        mqtt_user=None,
        mqtt_pass=None,
        mqtt_tls=False,
        mqtt_tls_insecure=False,
        mqtt_cafile="",
        mqtt_certfile="",
        mqtt_keyfile="",
        mqtt_topic="br",
        allowed_commands=(),
        mqtt_spool_dir="/tmp/spool",
        mqtt_queue_limit=100,
        serial_shared_secret=b"secret_1234567890123456",
        serial_retry_timeout=1.0,
        serial_response_timeout=5.0,
        serial_retry_attempts=5,
        serial_handshake_min_interval=1.0,
        serial_handshake_fatal_failures=10,
        debug_logging=False,
        allowed_policy=MagicMock(),
        file_system_root="/tmp/files",
        file_write_max_bytes=1024 * 1024,
        file_storage_quota_bytes=10 * 1024 * 1024,
        process_timeout=30,
        process_max_output_bytes=1024,
        process_max_concurrent=4,
        console_queue_limit_bytes=4096,
        mailbox_queue_limit=10,
        mailbox_queue_bytes_limit=1024,
        pending_pin_request_limit=5,
        reconnect_delay=5.0,
        status_interval=10.0,
        bridge_summary_interval=0.0,
        bridge_handshake_interval=0.0,
        metrics_enabled=False,
        metrics_host="127.0.0.1",
        metrics_port=9130,
        watchdog_enabled=False,
        watchdog_interval=5.0,
        allow_non_tmp_paths=False,
        topic_authorization=MagicMock()
    )


def create_fake_state():
    """Create a fake state object with real attributes to avoid MagicMock issues."""
    state = MagicMock()
    state.link_handshake_nonce = None
    state.link_is_synchronized = False
    state.handshake_rate_limit_until = 0.0
    state.handshake_failure_streak = 0
    state.handshake_attempts = 0
    state.handshake_successes = 0
    state.handshake_failures = 0
    state.handshake_backoff_until = 0.0
    state.handshake_fatal_count = 0
    state.handshake_fatal_reason = None
    state.handshake_fatal_detail = None
    state.handshake_fatal_unix = 0
    state.mcu_capabilities = None
    state.mqtt_topic_prefix = "br"
    state.handshake_last_duration = 0.0
    state.last_handshake_error = None
    state.link_expected_tag = b"tag"
    state.link_nonce_length = 8
    state.link_last_nonce_counter = 0
    return state


# --- mcubridge.common ---

def test_common_gaps():
    """Cover missed lines in common.py."""
    # Line 99: empty candidate in normalise_allowed_commands
    assert common.normalise_allowed_commands(["", "  ", "CMD"]) == ("cmd",)

    # Line 113: empty reason in encode_status_reason
    assert common.encode_status_reason(None) == b""
    assert common.encode_status_reason("") == b""


def test_parse_int_exception():
    """Test parse_int fallback on invalid types."""
    assert common.parse_int(None, 42) == 42
    assert common.parse_int("invalid", 42) == 42


def test_parse_float_exception():
    """Test parse_float fallback on invalid types."""
    assert common.parse_float(None, 3.14) == 3.14
    assert common.parse_float("invalid", 3.14) == 3.14


def test_log_hexdump_disabled():
    """Test log_hexdump when log level is disabled."""
    mock_logger = MagicMock()
    mock_logger.isEnabledFor.return_value = False
    common.log_hexdump(mock_logger, logging.DEBUG, "TEST", b"\x01\x02")
    mock_logger.log.assert_not_called()


def test_build_mqtt_properties_branches():
    """Cover remaining branches in build_mqtt_properties."""
    from mcubridge.mqtt.messages import QueuedPublish

    # Test with multiple properties to cover all lines
    msg = QueuedPublish(
        topic_name="t",
        payload=b"",
        message_expiry_interval=3600,
        user_properties=(("k", "v"),),
        content_type="application/json",
        payload_format_indicator=1,
        response_topic="resp",
        correlation_data=b"corr"
    )
    props = common.build_mqtt_properties(msg)
    assert props.MessageExpiryInterval == 3600
    assert props.UserProperty == [("k", "v")]
    assert props.ContentType == "application/json"
    assert props.PayloadFormatIndicator == 1
    assert props.ResponseTopic == "resp"
    assert props.CorrelationData == b"corr"

    # Cover branches where props are missing but at least one exists
    msg_skip_ct = QueuedPublish(topic_name="t", payload=b"", response_topic="resp")
    props_skip_ct = common.build_mqtt_properties(msg_skip_ct)
    assert props_skip_ct is not None
    assert props_skip_ct.ResponseTopic == "resp"
    assert not hasattr(props_skip_ct, "ContentType")

    msg_skip_rt = QueuedPublish(topic_name="t", payload=b"", content_type="text/plain")
    props_skip_rt = common.build_mqtt_properties(msg_skip_rt)
    assert props_skip_rt is not None
    assert props_skip_rt.ContentType == "text/plain"
    assert not hasattr(props_skip_rt, "ResponseTopic")


@patch("mcubridge.common.os.path.exists")
@patch("mcubridge.common.uci.Uci")
def test_get_uci_config_openwrt_failures(mock_uci_class, mock_exists):
    """Cover UCI failure paths on OpenWrt."""
    mock_exists.return_value = True

    mock_cursor = MagicMock()
    mock_uci_class.return_value.__enter__.return_value = mock_cursor

    from mcubridge.common import uci as common_uci

    # Case 1: UciException -> re-raises as RuntimeError
    mock_cursor.get_all.side_effect = common_uci.UciException("Failure")
    with pytest.raises(RuntimeError, match="Critical UCI failure"):
        common.get_uci_config()

    # Case 2: Missing section -> RuntimeError
    mock_cursor.get_all.side_effect = None
    mock_cursor.get_all.return_value = None
    with pytest.raises(RuntimeError, match="missing!"):
        common.get_uci_config()

    # Case 3: OSError -> RuntimeError (Line 218-219)
    mock_uci_class.side_effect = OSError("Disk error")
    with pytest.raises(RuntimeError, match="Critical UCI failure"):
        common.get_uci_config()


# --- mcubridge.metrics ---

@pytest.mark.asyncio
async def test_publish_metrics_error_recovery():
    """Cover exception path in publish_metrics loop."""
    state = MagicMock()
    # First call fails, second succeeds, third cancels
    state.build_metrics_snapshot.side_effect = [Exception("fail"), {"cpu": 1}, asyncio.CancelledError]

    enqueue = AsyncMock()

    with patch("asyncio.sleep", return_value=None):
        try:
            await metrics.publish_metrics(state, enqueue, interval=0.1)
        except (asyncio.CancelledError, Exception):
            pass
    assert state.build_metrics_snapshot.call_count >= 1


def test_build_metrics_message_branches():
    """Cover branches in _build_metrics_message."""
    state = MagicMock()
    state.mqtt_topic_prefix = "br"

    # Test spool degraded
    snapshot = {"mqtt_spool_degraded": True, "mqtt_spool_failure_reason": "disk-full"}
    msg = metrics._build_metrics_message(state, snapshot, expiry_seconds=60)
    assert ("bridge-spool", "disk-full") in msg.user_properties

    # Test file status - quota-blocked
    snapshot = {"file_storage_limit_rejections": 1}
    msg = metrics._build_metrics_message(state, snapshot, expiry_seconds=60)
    assert ("bridge-files", "quota-blocked") in msg.user_properties

    # Test file status - write-limit
    snapshot = {"file_write_limit_rejections": 1}
    msg = metrics._build_metrics_message(state, snapshot, expiry_seconds=60)
    assert ("bridge-files", "write-limit") in msg.user_properties

    # Test watchdog
    snapshot = {"watchdog_enabled": True, "watchdog_interval": 5.0}
    msg = metrics._build_metrics_message(state, snapshot, expiry_seconds=60)
    assert ("bridge-watchdog-enabled", "1") in msg.user_properties
    assert ("bridge-watchdog-interval", "5.0") in msg.user_properties


@pytest.mark.asyncio
async def test_emit_bridge_snapshot_errors():
    """Cover error paths in _emit_bridge_snapshot."""
    state = MagicMock()
    state.build_bridge_snapshot.side_effect = ValueError("Boom")
    enqueue = AsyncMock()

    # Should log error and return (Line 164)
    with patch("mcubridge.metrics.logger.critical") as mock_crit:
        state.build_bridge_snapshot.side_effect = AttributeError("Boom")
        await metrics._emit_bridge_snapshot(state, enqueue, flavor="summary")
        assert mock_crit.called

    # Test CancelledError (Line 156)
    state.build_bridge_snapshot.side_effect = asyncio.CancelledError
    with pytest.raises(asyncio.CancelledError):
        await metrics._emit_bridge_snapshot(state, enqueue, flavor="summary")


@pytest.mark.asyncio
async def test_bridge_snapshot_loop_gaps():
    """Cover gaps in _bridge_snapshot_loop."""
    state = MagicMock()
    enqueue = AsyncMock()

    # Cover initial emit error paths (Lines 182-187)
    with (
        patch("mcubridge.metrics._emit_bridge_snapshot", side_effect=OSError("Boom")),
        patch("asyncio.sleep", side_effect=asyncio.CancelledError)
    ):
        with pytest.raises(asyncio.CancelledError):
            await metrics._bridge_snapshot_loop(state, enqueue, flavor="summary", seconds=10)

    with (
        patch("mcubridge.metrics._emit_bridge_snapshot", side_effect=AttributeError("Boom")),
        patch("asyncio.sleep", side_effect=asyncio.CancelledError)
    ):
        with pytest.raises(asyncio.CancelledError):
            await metrics._bridge_snapshot_loop(state, enqueue, flavor="summary", seconds=10)


@pytest.mark.asyncio
async def test_publish_metrics_gaps():
    """Cover gaps in publish_metrics."""
    state = MagicMock()
    enqueue = AsyncMock()

    # Line 205: interval <= 0
    with pytest.raises(ValueError, match="greater than zero"):
        await metrics.publish_metrics(state, enqueue, interval=0)

    # Line 212-213: CancelledError in initial emit
    with patch("mcubridge.metrics._emit_metrics_snapshot", side_effect=asyncio.CancelledError):
        with pytest.raises(asyncio.CancelledError):
            await metrics.publish_metrics(state, enqueue, interval=10)

    # Line 215, 217: Initial emit errors
    with (
        patch("mcubridge.metrics._emit_metrics_snapshot", side_effect=OSError("Boom")),
        patch("asyncio.sleep", side_effect=asyncio.CancelledError)
    ):
        with pytest.raises(asyncio.CancelledError):
            await metrics.publish_metrics(state, enqueue, interval=10)

    # Line 223-230: Loop errors
    with (
        patch("mcubridge.metrics._emit_metrics_snapshot", side_effect=[None, OSError("Boom")]),
        patch("asyncio.sleep", side_effect=[None, asyncio.CancelledError])
    ):
        with pytest.raises(asyncio.CancelledError):
            await metrics.publish_metrics(state, enqueue, interval=10)


@pytest.mark.asyncio
async def test_publish_bridge_snapshots_disabled():
    """Cover the 'disabled' path in publish_bridge_snapshots."""
    state = MagicMock()
    enqueue = AsyncMock()

    with patch("asyncio.Event.wait", new_callable=AsyncMock) as mock_wait:
        # Mock wait to return immediately to avoid hanging the test
        await metrics.publish_bridge_snapshots(state, enqueue, summary_interval=0, handshake_interval=0)
        assert mock_wait.called


@pytest.mark.asyncio
async def test_publish_bridge_snapshots_exc_group():
    """Cover ExceptionGroup in publish_bridge_snapshots (Line 257->266)."""
    state = MagicMock()
    enqueue = AsyncMock()

    # Trigger ExceptionGroup by having one task fail
    with (
        patch("mcubridge.metrics._bridge_snapshot_loop", side_effect=RuntimeError("Group Boom")),
        pytest.raises(ExceptionGroup)
    ):
        await metrics.publish_bridge_snapshots(state, enqueue, summary_interval=10, handshake_interval=0)


def test_runtime_state_collector_collect_histogram_branches():
    """Cover histogram branches in _RuntimeStateCollector."""
    state = MagicMock()
    # Latency data with buckets (Lines 281-282, 300-305)
    state.build_metrics_snapshot.return_value = {
        "serial_latency": {
            "count": 1,
            "sum_ms": 100.0,
            "buckets": {
                "le_100ms": 1,
                "invalid": 0,
                "le_NaNms": 0,
            }
        }
    }
    collector = metrics._RuntimeStateCollector(state)
    results = list(collector.collect())
    assert any("mcubridge_serial_rpc_latency_seconds" in str(r.name) for r in results)


def test_metrics_flatten_branches():
    """Cover branches in _flatten."""
    state = MagicMock()
    collector = metrics._RuntimeStateCollector(state)

    # Test None
    results = list(collector._flatten("test", None))
    assert results == [("info", "test", "null")]

    # Test str
    results = list(collector._flatten("test", "val"))
    assert results == [("info", "test", "val")]


@pytest.mark.asyncio
async def test_prometheus_exporter_gaps():
    """Cover gaps in PrometheusExporter."""
    state = MagicMock()
    exporter = metrics.PrometheusExporter(state, "127.0.0.1", 0)

    # Coverage for start() while already started (Line 422)
    exporter._server = MagicMock()
    await exporter.start()

    # Coverage for stop() while already stopped (Line 444)
    exporter._server = None
    await exporter.stop()

    # Coverage for _handle_client error paths
    mock_reader = AsyncMock()
    mock_writer = AsyncMock()

    # Case: Empty request line
    mock_reader.readline.return_value = b""
    await exporter._handle_client(mock_reader, mock_writer)

    # Case: Short request line (Line 468)
    mock_reader.readline.return_value = b"GET\n"
    await exporter._handle_client(mock_reader, mock_writer)

    # Case: IndexError/ValueError in handler (Line 488-493)
    mock_reader.readline.side_effect = [b"GET /metrics HTTP/1.1\n", b"\n"]
    with patch.object(exporter, "_render_metrics", side_effect=IndexError("Boom")):
        await exporter._handle_client(mock_reader, mock_writer)


# --- mcubridge.daemon ---

@pytest.mark.asyncio
@patch("mcubridge.daemon.cleanup_status_file")
async def test_daemon_run_lifecycle_coverage(mock_cleanup):
    """Cover cancellation and error paths in BridgeDaemon.run."""
    from mcubridge.daemon import BridgeDaemon

    config = create_fake_config()

    daemon_obj = BridgeDaemon(config)

    # Cover cancellation path
    with patch("asyncio.TaskGroup.__aenter__", side_effect=asyncio.CancelledError):
        await daemon_obj.run()

    # Cover exception group / generic exception path (Line 217-225)
    mock_cleanup.reset_mock()
    with (
        patch("asyncio.TaskGroup.__aenter__", side_effect=RuntimeError("Generic failure")),
        pytest.raises((RuntimeError, ExceptionGroup))
    ):
        # On Python 3.11+, TaskGroup raises ExceptionGroup
        await daemon_obj.run()
    assert mock_cleanup.call_count >= 1


def test_daemon_main_abort():
    """Cover main() error handling paths."""
    with (
        patch("mcubridge.daemon.load_runtime_config", side_effect=RuntimeError("Abort")),
        patch("mcubridge.daemon.configure_logging"),
        patch("sys.exit") as mock_exit
    ):
        with pytest.raises(RuntimeError, match="Abort"):
            daemon.main()
        assert not mock_exit.called


def test_daemon_main_base_exception():
    """Cover BaseException in daemon.main."""
    config = create_fake_config()
    with (
        patch("mcubridge.daemon.load_runtime_config", return_value=config),
        patch("mcubridge.daemon.configure_logging"),
        patch("mcubridge.daemon.BridgeDaemon", side_effect=BaseException("Fatal")),
        patch("sys.exit") as mock_exit
    ):
        daemon.main()
        # Coverage for line 276
        assert mock_exit.called


@pytest.mark.asyncio
async def test_daemon_factories():
    """Cover factory methods in BridgeDaemon."""
    config = create_fake_config()
    daemon_obj = daemon.BridgeDaemon(config)

    # Test branch where secret is missing
    config_no_secret = create_fake_config()
    config_no_secret.serial_shared_secret = b""
    daemon.BridgeDaemon(config_no_secret)

    with (
        patch("mcubridge.daemon.SerialTransport.run", new_callable=AsyncMock) as mock_serial,
        patch("mcubridge.daemon.mqtt_task", new_callable=AsyncMock) as mock_mqtt,
        patch("mcubridge.daemon.status_writer", new_callable=AsyncMock) as mock_status,
        patch("mcubridge.daemon.publish_metrics", new_callable=AsyncMock) as mock_metrics,
        patch("mcubridge.daemon.publish_bridge_snapshots", new_callable=AsyncMock) as mock_snapshots
    ):
        await daemon_obj._run_serial_link()
        assert mock_serial.called

        await daemon_obj._run_mqtt_link()
        assert mock_mqtt.called

        await daemon_obj._run_status_writer()
        assert mock_status.called

        await daemon_obj._run_metrics_publisher()
        assert mock_metrics.called

        await daemon_obj._run_bridge_snapshots()
        assert mock_snapshots.called


# --- mcubridge.config.settings ---

def test_settings_load_runtime_config_coverage():
    """Cover load_runtime_config body."""
    from mcubridge.config.settings import load_runtime_config

    raw_cfg = {
        "serial_port": "/dev/ttyFake",
        "serial_shared_secret": "secret_12345678",
        "debug": "1",
        "watchdog_enabled": "1",
        "mqtt_tls": "0",
        "allow_non_tmp_paths": "1"
    }

    with patch("mcubridge.config.settings._load_raw_config", return_value=raw_cfg):
        config = load_runtime_config()
        assert config.debug_logging is True
        assert config.watchdog_enabled is True
        assert config.mqtt_tls is False
        assert config.allow_non_tmp_paths is True


def test_settings_validation_errors():
    """Cover validation errors in RuntimeConfig."""
    from mcubridge.config.settings import RuntimeConfig
    cfg_dict = {
        "serial_port": "/dev/ttyS0",
        "serial_baud": 115200,
        "serial_safe_baud": 9600,
        "mqtt_host": "localhost",
        "mqtt_port": 1883,
        "mqtt_user": None,
        "mqtt_pass": None,
        "mqtt_tls": True,
        "mqtt_cafile": None,
        "mqtt_certfile": None,
        "mqtt_keyfile": None,
        "mqtt_topic": "br",
        "allowed_commands": (),
        "file_system_root": "/tmp/files",
        "process_timeout": 30,
        "serial_shared_secret": b"secret_12345678"
    }

    # Test insecure placeholder
    bad_cfg = cfg_dict.copy()
    bad_cfg["serial_shared_secret"] = b"changeme123"
    with pytest.raises(ValueError, match="insecure"):
        RuntimeConfig(**bad_cfg)

    # Test empty secret
    bad_cfg = cfg_dict.copy()
    bad_cfg["serial_shared_secret"] = b""
    with pytest.raises(ValueError, match="configured"):
        RuntimeConfig(**bad_cfg)

    # Test short secret
    bad_cfg = cfg_dict.copy()
    bad_cfg["serial_shared_secret"] = b"short"
    with pytest.raises(ValueError, match="at least 8 bytes"):
        RuntimeConfig(**bad_cfg)

    # Test non-tmp fs root
    bad_cfg = cfg_dict.copy()
    bad_cfg["file_system_root"] = "/home/user"
    with pytest.raises(ValueError, match="FLASH PROTECTION"):
        RuntimeConfig(**bad_cfg)

    # Test empty topic
    bad_cfg = cfg_dict.copy()
    bad_cfg["mqtt_topic"] = "/"
    with pytest.raises(ValueError, match="at least one segment"):
        RuntimeConfig(**bad_cfg)

    # Test tls_insecure warning
    with patch("mcubridge.config.settings.logger.warning") as mock_warn:
        cfg_insecure = cfg_dict.copy()
        cfg_insecure["mqtt_tls_insecure"] = True
        RuntimeConfig(**cfg_insecure)
        assert any("hostname verification is disabled" in str(arg) for arg in mock_warn.call_args[0])

    # Test quota < write_max
    bad_cfg = cfg_dict.copy()
    bad_cfg["file_write_max_bytes"] = 1000
    bad_cfg["file_storage_quota_bytes"] = 500
    with pytest.raises(ValueError, match="greater than or equal to"):
        RuntimeConfig(**bad_cfg)


def test_settings_load_raw_config_error():
    """Cover error path in _load_raw_config."""
    from mcubridge.config.settings import _load_raw_config
    with patch("mcubridge.config.settings.get_uci_config", side_effect=OSError("Boom")):
        res = _load_raw_config()
        assert isinstance(res, dict)

    # Test empty uci values
    with patch("mcubridge.config.settings.get_uci_config", return_value={}):
        res = _load_raw_config()
        assert isinstance(res, dict)


def test_settings_normalize_path_empty():
    """Cover empty path in _normalize_path."""
    from mcubridge.config.settings import RuntimeConfig
    with pytest.raises(ValueError, match="non-empty path"):
        RuntimeConfig._normalize_path("", field_name="test", require_absolute=True)

    with pytest.raises(ValueError, match="absolute path"):
        RuntimeConfig._normalize_path("relative/path", field_name="test", require_absolute=True)


def test_configure_logging_settings_dead_code():
    """Cover the dead configure_logging in settings.py."""
    from mcubridge.config.settings import configure_logging as settings_log
    config = create_fake_config()

    # Create a real SysLogHandler mock that can be called
    mock_syslog = MagicMock()

    # Case: Syslog OK
    with (
        patch("mcubridge.config.settings.os.path.exists", return_value=True),
        patch("mcubridge.config.settings.logging.handlers.SysLogHandler", return_value=mock_syslog)
    ):
        settings_log(config)

    # Case: Syslog OSError
    with (
        patch("mcubridge.config.settings.os.path.exists", return_value=True),
        patch("mcubridge.config.settings.logging.handlers.SysLogHandler", side_effect=OSError("fail")),
        patch("sys.stderr.write") as mock_write
    ):
        settings_log(config)
        assert mock_write.called

    # Case: No Syslog
    with patch("mcubridge.config.settings.os.path.exists", return_value=False):
        settings_log(config)


def test_logging_gaps():
    """Cover gaps in mcubridge.config.logging."""
    from mcubridge.config.logging import StructuredLogFormatter, _serialise_value, _build_handler

    # Test _serialise_value with unknown type
    assert _serialise_value(set()) == "set()"

    # Test StructuredLogFormatter with extras and exception
    formatter = StructuredLogFormatter()
    record = logging.LogRecord(
        name="mcubridge.test",
        level=logging.INFO,
        pathname="test.py",
        lineno=1,
        msg="test message",
        args=(),
        exc_info=None
    )
    record.custom_extra = "extra_val"

    res = formatter.format(record)
    assert "extra_val" in res

    # Test with exception
    try:
        raise ValueError("Boom")
    except ValueError:
        record.exc_info = sys.exc_info()

    res = formatter.format(record)
    assert "exception" in res
    assert "Boom" in res

    # Test _build_handler branch where SYSLOG_SOCKET is NOT /dev/log
    with patch("mcubridge.config.logging.SYSLOG_SOCKET", Path("/tmp/log")):
        _build_handler()

    # Test _build_handler branch where /dev/log does not exist
    with patch("mcubridge.config.logging.Path.exists", return_value=False):
        handler = _build_handler()
        assert isinstance(handler, logging.StreamHandler)


# --- mcubridge.services.handshake ---

@pytest.mark.asyncio
async def test_handshake_send_failures():
    """Cover send failures in SerialHandshakeManager."""
    config = create_fake_config()
    state = create_fake_state()
    sender = AsyncMock(return_value=False)
    enqueue = AsyncMock()
    timing = MagicMock()
    ack = AsyncMock()

    comp = SerialHandshakeManager(
        config=config,
        state=state,
        serial_timing=timing,
        send_frame=sender,
        enqueue_mqtt=enqueue,
        acknowledge_frame=ack
    )

    # Test LINK_RESET failure
    res = await comp.synchronize()
    assert res is False

    # Test LINK_SYNC failure
    sender.side_effect = [True, False]
    res = await comp.synchronize()
    assert res is False


@pytest.mark.asyncio
async def test_handshake_sync_timeout():
    """Cover LINK_SYNC confirmation timeout."""
    config = create_fake_config()
    state = create_fake_state()
    state.link_handshake_nonce = b"nonce"
    state.link_is_synchronized = False
    sender = AsyncMock(return_value=True)
    enqueue = AsyncMock()
    timing = MagicMock()
    timing.response_timeout_seconds = 0.1
    ack = AsyncMock()

    comp = SerialHandshakeManager(
        config=config,
        state=state,
        serial_timing=timing,
        send_frame=sender,
        enqueue_mqtt=enqueue,
        acknowledge_frame=ack
    )

    with patch("asyncio.sleep", return_value=None):
        res = await comp.synchronize()
        assert res is False


@pytest.mark.asyncio
async def test_handshake_unexpected_resp():
    """Cover unexpected LINK_SYNC_RESP."""
    config = create_fake_config()
    state = create_fake_state()
    state.link_handshake_nonce = None
    sender = AsyncMock()
    enqueue = AsyncMock()
    timing = MagicMock()
    ack = AsyncMock()

    comp = SerialHandshakeManager(
        config=config,
        state=state,
        serial_timing=timing,
        send_frame=sender,
        enqueue_mqtt=enqueue,
        acknowledge_frame=ack
    )
    res = await comp.handle_link_sync_resp(b"payload")
    assert res is False


@pytest.mark.asyncio
async def test_handshake_rate_limit():
    """Cover rate limiting in LINK_SYNC_RESP."""
    config = create_fake_config()
    config.serial_handshake_min_interval = 10.0
    state = create_fake_state()
    state.link_handshake_nonce = b"nonce"
    state.handshake_rate_limit_until = time.monotonic() + 10.0
    sender = AsyncMock()
    enqueue = AsyncMock()
    timing = MagicMock()
    ack = AsyncMock()

    comp = SerialHandshakeManager(
        config=config,
        state=state,
        serial_timing=timing,
        send_frame=sender,
        enqueue_mqtt=enqueue,
        acknowledge_frame=ack
    )
    res = await comp.handle_link_sync_resp(b"nonce" + b"tag")
    assert res is False


@pytest.mark.asyncio
async def test_handshake_fetch_capabilities_retry_error():
    """Cover tenacity retry error in _fetch_capabilities."""
    config = create_fake_config()
    state = create_fake_state()
    sender = AsyncMock(return_value=True)
    enqueue = AsyncMock()
    timing = MagicMock()
    timing.response_timeout_seconds = 0.1
    ack = AsyncMock()

    comp = SerialHandshakeManager(
        config=config,
        state=state,
        serial_timing=timing,
        send_frame=sender,
        enqueue_mqtt=enqueue,
        acknowledge_frame=ack
    )

    # Mock asyncio.wait_for to always timeout
    with patch("asyncio.wait_for", side_effect=asyncio.TimeoutError):
        res = await comp._fetch_capabilities()
        assert res is False


def test_handshake_parse_capabilities_errors():
    """Cover error paths in _parse_capabilities."""
    config = create_fake_config()
    state = create_fake_state()
    comp = SerialHandshakeManager(
        config=config,
        state=state,
        serial_timing=MagicMock(),
        send_frame=AsyncMock(),
        enqueue_mqtt=AsyncMock(),
        acknowledge_frame=AsyncMock()
    )

    # Short payload
    comp._parse_capabilities(b"short")
    assert state.mcu_capabilities is None

    # Unpack error
    with patch("struct.unpack", side_effect=struct.error):
        comp._parse_capabilities(b"12345678")

def test_handshake_calculate_tag_no_secret():
    """Cover empty secret in calculate_handshake_tag."""
    assert SerialHandshakeManager.calculate_handshake_tag(None, b"nonce") == b""


# --- mcubridge.__init__ ---

def test_init_check_dependencies_direct():
    """Directly test _check_dependencies when attr is missing."""
    import mcubridge

    with (
        patch("mcubridge.logger.critical") as mock_crit,
        patch("sys.exit") as mock_exit,
        patch("mcubridge.hasattr", return_value=False)
    ):
        mcubridge._check_dependencies()
        assert mock_crit.called
        mock_exit.assert_called_with(1)


# --- mcubridge/services/handshake.py ---

def test_derive_serial_timing_clamping():
    """Cover clamping and limits in derive_serial_timing."""
    cfg = create_fake_config()
    cfg.serial_retry_timeout = 10000.0
    timing = derive_serial_timing(cfg)
    assert timing.ack_timeout_ms > 0

    cfg.serial_retry_timeout = -1.0
    timing = derive_serial_timing(cfg)
    assert timing.ack_timeout_ms > 0
