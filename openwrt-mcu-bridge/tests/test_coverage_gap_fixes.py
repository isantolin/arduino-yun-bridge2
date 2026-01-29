"Tests to close coverage gaps in the mcubridge ecosystem."

import logging
import asyncio
import struct
import time
import sys
import errno
from pathlib import Path
from unittest.mock import MagicMock, patch, AsyncMock
from types import SimpleNamespace
import pytest
import tenacity
import aiomqtt
import mcubridge.common as common
import mcubridge.metrics as metrics
import mcubridge.daemon as daemon
import mcubridge.security as security
import mcubridge.protocol.topics as topics
import mcubridge.transport.mqtt as mqtt
import mcubridge.state.status as status
import mcubridge.services.dispatcher as dispatcher
import mcubridge.state.context as context
import mcubridge.mqtt.spool as spool
import mcubridge.services.handshake as handshake
from mcubridge.rpc.protocol import Command, Status
from mcubridge.protocol.topics import Topic, TopicRoute
from mcubridge.services.handshake import SerialHandshakeManager, derive_serial_timing
from mcubridge.mqtt.messages import QueuedPublish


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
        topic_authorization=MagicMock(),
        tls_enabled=False,
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
    state.serial_writer = None
    state.mqtt_publish_queue = asyncio.Queue()
    state.mqtt_queue_limit = 100
    state.mqtt_dropped_messages = 0
    state.mqtt_drop_counts = {}
    state.mqtt_spooled_messages = 0
    state.mqtt_spooled_replayed = 0
    state.mqtt_spool_errors = 0
    state.mqtt_spool_degraded = False
    state.mqtt_spool_failure_reason = None
    state.mqtt_spool_retry_attempts = 0
    state.mqtt_spool_backoff_until = 0.0
    state.mqtt_spool_last_error = None
    state.mqtt_spool_recoveries = 0
    state.mqtt_spool = None
    state.file_system_root = "/tmp"
    state.file_storage_bytes_used = 0
    state.file_storage_quota_bytes = 1000
    state.file_write_max_bytes = 500
    state.file_write_limit_rejections = 0
    state.file_storage_limit_rejections = 0
    state.datastore = {}
    state.mailbox_queue = []
    state.mailbox_queue_bytes = 0
    state.mailbox_dropped_messages = 0
    state.mailbox_dropped_bytes = 0
    state.mailbox_truncated_messages = 0
    state.mailbox_truncated_bytes = 0
    state.mailbox_incoming_dropped_messages = 0
    state.mailbox_incoming_dropped_bytes = 0
    state.mailbox_incoming_truncated_messages = 0
    state.mailbox_incoming_truncated_bytes = 0
    state.mcu_is_paused = False
    state.console_to_mcu_queue = []
    state.console_queue_bytes = 0
    state.console_dropped_chunks = 0
    state.console_dropped_bytes = 0
    state.console_truncated_chunks = 0
    state.console_truncated_bytes = 0
    state.watchdog_enabled = False
    state.watchdog_interval = 5.0
    state.watchdog_beats = 0
    state.last_watchdog_beat = 0.0
    state.running_processes = {}
    state.allowed_commands = []
    state.last_handshake_unix = 0.0
    state.serial_flow_stats = MagicMock()
    state.serial_flow_stats.as_dict.return_value = {}
    state.supervisor_stats = {}
    state.mcu_version = (1, 0)
    state.flush_mqtt_spool = AsyncMock()
    state.build_bridge_snapshot.return_value = {}
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
    assert common.parse_int("invalid", 42) == 42


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
        correlation_data=b"corr",
    )
    props = common.build_mqtt_properties(msg)
    assert props.MessageExpiryInterval == 3600
    assert props.UserProperty == [("k", "v")]
    assert props.ContentType == "application/json"
    assert props.PayloadFormatIndicator == 1
    assert props.ResponseTopic == "resp"
    assert props.CorrelationData == b"corr"

    # Cover branches where props are missing but at least one exists
    # Test skipping content_type (136->139)
    msg_skip_ct = QueuedPublish(topic_name="t", payload=b"", response_topic="resp")
    props_skip_ct = common.build_mqtt_properties(msg_skip_ct)
    assert props_skip_ct is not None
    assert props_skip_ct.ResponseTopic == "resp"
    assert not hasattr(props_skip_ct, "ContentType")

    # Test skipping response_topic (145->148)
    msg_skip_rt = QueuedPublish(topic_name="t", payload=b"", content_type="text/plain")
    props_skip_rt = common.build_mqtt_properties(msg_skip_rt)
    assert props_skip_rt is not None
    assert props_skip_rt.ContentType == "text/plain"
    assert not hasattr(props_skip_rt, "ResponseTopic")


@patch("mcubridge.common.os.path.exists")
@patch("mcubridge.common.uci.Uci")
def test_get_uci_config_openwrt_failures(mock_uci_class, mock_exists):
    """Cover UCI failure paths on OpenWrt."""
    # Force is_openwrt to True
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

    # Case 3: OSError -> RuntimeError (Line 214-216 if is_openwrt=True)
    mock_uci_class.side_effect = OSError("Disk error")
    with pytest.raises(RuntimeError, match="Critical UCI failure"):
        common.get_uci_config()

    # Case 4: Non-OpenWrt OSError (Line 218-219)
    mock_exists.return_value = False
    mock_uci_class.side_effect = OSError("Disk error")
    res = common.get_uci_config()
    assert isinstance(res, dict)


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
    state.build_bridge_snapshot.return_value = {}
    state.build_bridge_snapshot.side_effect = ValueError("Boom")
    enqueue = AsyncMock()

    # Should log error and return (Line 158)
    with patch("mcubridge.metrics.logger.error") as mock_err:
        state.build_bridge_snapshot.side_effect = ValueError("Boom")
        await metrics._emit_bridge_snapshot(state, enqueue, flavor="summary")
        assert mock_err.called

    # Should log critical and return (Line 164)
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
        patch("asyncio.sleep", side_effect=asyncio.CancelledError),
    ):
        with pytest.raises(asyncio.CancelledError):
            await metrics._bridge_snapshot_loop(state, enqueue, flavor="summary", seconds=10)

    # Line 191 loop emit
    with (
        patch("mcubridge.metrics._emit_bridge_snapshot", side_effect=[None, None]),
        patch("asyncio.sleep", side_effect=[None, asyncio.CancelledError]),
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
        patch("asyncio.sleep", side_effect=asyncio.CancelledError),
    ):
        with pytest.raises(asyncio.CancelledError):
            await metrics.publish_metrics(state, enqueue, interval=10)

    with (
        patch("mcubridge.metrics._emit_metrics_snapshot", side_effect=AttributeError("Boom")),
        patch("asyncio.sleep", side_effect=asyncio.CancelledError),
    ):
        with pytest.raises(asyncio.CancelledError):
            await metrics.publish_metrics(state, enqueue, interval=10)

    # Line 223-230: Loop errors
    # Line 226: CancelledError in loop
    with (
        patch(
            "mcubridge.metrics._emit_metrics_snapshot",
            side_effect=[None, asyncio.CancelledError, asyncio.CancelledError],
        ),
        patch("asyncio.sleep", side_effect=[None, None]),
    ):
        with pytest.raises(asyncio.CancelledError):
            await metrics.publish_metrics(state, enqueue, interval=10)

    # Line 229-230: TypeError/ValueError in loop
    with (
        patch(
            "mcubridge.metrics._emit_metrics_snapshot",
            side_effect=[None, ValueError("Boom"), asyncio.CancelledError],
        ),
        patch("asyncio.sleep", side_effect=[None, None, asyncio.CancelledError]),
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
        pytest.raises(ExceptionGroup),
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
                "invalid": 0,  # Should be skipped (Line 301-302)
                "le_NaNms": 0,  # Should be skipped (Line 304 ValueError branch)
            },
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

    # Coverage for start() socket path (Lines 429-437)
    exporter._server = None
    with patch("asyncio.start_server", new_callable=AsyncMock) as mock_start:
        mock_server = MagicMock()
        mock_sock = MagicMock()
        mock_sock.getsockname.return_value = ("127.0.0.1", 12345)
        mock_server.sockets = [mock_sock]
        mock_start.return_value = mock_server
        await exporter.start()
        assert exporter.port == 12345

    # Coverage for stop() while already stopped (Line 444)
    exporter._server = None
    await exporter.stop()

    # Coverage for _handle_client error paths
    mock_reader = AsyncMock()
    # Use AsyncMock for writer to handle await writer.wait_closed()
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
        pytest.raises((RuntimeError, ExceptionGroup)),
    ):
        # On Python 3.11+, TaskGroup raises ExceptionGroup
        await daemon_obj.run()
    assert mock_cleanup.call_count >= 1


def test_daemon_main_abort():
    """Cover main() error handling paths."""
    with (
        patch("mcubridge.daemon.load_runtime_config", side_effect=RuntimeError("Abort")),
        patch("mcubridge.daemon.configure_logging"),
        patch("sys.exit") as mock_exit,
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
        patch("sys.exit") as mock_exit,
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
        patch("mcubridge.daemon.publish_bridge_snapshots", new_callable=AsyncMock) as mock_snapshots,
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

    raw_cfg = common.get_default_config()
    raw_cfg.update(
        {
            "serial_port": "/dev/ttyFake",
            "serial_baud": 57600,
            "serial_safe_baud": 9600,
            "mqtt_host": "localhost",
            "mqtt_port": 1883,
            "mqtt_topic": "bridge",
            "serial_shared_secret": "secret_12345678",
            "debug": "1",
            "watchdog_enabled": "1",
            "mqtt_tls": "0",
            "allow_non_tmp_paths": "1",
        }
    )

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
        "serial_shared_secret": b"secret_12345678",
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
        patch("mcubridge.config.settings.logging.handlers.SysLogHandler", return_value=mock_syslog),
    ):
        settings_log(config)

    # Case: Syslog OSError
    with (
        patch("mcubridge.config.settings.os.path.exists", return_value=True),
        patch("mcubridge.config.settings.logging.handlers.SysLogHandler", side_effect=OSError("fail")),
        patch("sys.stderr.write") as mock_write,
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
        exc_info=None,
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


# --- mcubridge.security ---


def test_security_gaps():
    """Cover gaps in security.py."""
    # secure_zero with memoryview
    buf = bytearray(b"test")
    mv = memoryview(buf)
    security.secure_zero(mv)
    assert buf == bytearray(4)

    # secure_zero_bytes_copy
    res = security.secure_zero_bytes_copy(b"test")
    assert res == b"\x00\x00\x00\x00"

    # extract_nonce_counter ValueError
    with pytest.raises(ValueError, match="Nonce must be 16 bytes"):
        security.extract_nonce_counter(b"short")

    # validate_nonce_counter ValueError path
    res, _ = security.validate_nonce_counter(b"short", 0)
    assert res is False

    # validate_nonce_counter replay branch (current <= last_counter)
    # Correct nonce is 16 bytes. Let's use a real one.
    nonce, next_counter = security.generate_nonce_with_counter(10)
    res, _ = security.validate_nonce_counter(nonce, 10)
    assert res is True

    # Replay
    res, _ = security.validate_nonce_counter(nonce, 11)
    assert res is False


# --- mcubridge.protocol.topics ---


def test_topics_gaps():
    """Cover gaps in topics.py."""
    # _split_segments not path
    assert topics._split_segments("") == ()

    # topic_path cleaned False branch
    res = topics.topic_path("prefix", "topic", "", " ", "segment")
    assert "segment" in res

    # parse_topic ValueError (invalid topic)
    assert topics.parse_topic("br", "br/invalid/topic") is None

    # TopicRoute identification/remainder
    from mcubridge.protocol.topics import TopicRoute, Topic

    tr_empty = TopicRoute("raw", "prefix", Topic.SYSTEM, ())
    assert tr_empty.identifier == ""
    assert tr_empty.remainder == ()


# --- mcubridge.state.status ---


@pytest.mark.asyncio
async def test_status_writer_gaps():
    """Cover gaps in status.py."""
    state = create_fake_state()

    # Line 112-117: CancelledError during thread await
    with patch("asyncio.to_thread", side_effect=asyncio.CancelledError):
        with pytest.raises(asyncio.CancelledError):
            await status.status_writer(state, 10)

    # Line 126-127: cleanup_status_file OSError
    # Mock the STATUS_FILE directly in the module
    with patch("mcubridge.state.status.STATUS_FILE") as mock_file:
        status.cleanup_status_file()
        assert mock_file.unlink.called

        mock_file.unlink.side_effect = OSError("fail")
        status.cleanup_status_file()  # Should catch OSError

    # _write_status_file (Lines 131-140)
    with (
        patch("mcubridge.state.status.NamedTemporaryFile") as mock_ntf,
        patch("mcubridge.state.status.Path") as mock_path,
    ):
        payload = {"test": 1}
        status._write_status_file(payload)
        assert mock_ntf.called
        assert mock_path.called


# --- mcubridge.transport.mqtt ---


def test_mqtt_configure_tls_gaps():
    """Cover gaps in mqtt._configure_tls."""
    config = create_fake_config()
    config.tls_enabled = True
    config.mqtt_cafile = "/tmp/fake_ca"

    # Case: CA file missing
    with patch("mcubridge.transport.mqtt.Path.exists", return_value=False):
        with pytest.raises(RuntimeError, match="CA file missing"):
            mqtt._configure_tls(config)

    # Case: SSLError/ValueError during setup
    # Make sure CA file check passes
    config.mqtt_cafile = ""  # Avoid missing CA check
    with (
        patch("mcubridge.transport.mqtt.Path.exists", return_value=True),
        patch("ssl.create_default_context", side_effect=ValueError("Boom")),
        pytest.raises(RuntimeError, match="TLS setup failed"),
    ):
        mqtt._configure_tls(config)


@pytest.mark.asyncio
async def test_mqtt_publisher_loop_gaps():
    """Cover gaps in _mqtt_publisher_loop."""
    state = create_fake_state()
    mock_client = AsyncMock()

    from mcubridge.mqtt.messages import QueuedPublish

    msg = QueuedPublish("t", b"p")
    await state.mqtt_publish_queue.put(msg)

    # Case: CancelledError (Line 80-85)
    mock_client.publish.side_effect = asyncio.CancelledError
    with pytest.raises(asyncio.CancelledError):
        await mqtt._mqtt_publisher_loop(state, mock_client)
    # Verify message was requeued
    assert state.mqtt_publish_queue.qsize() == 1

    # Case: MqttError (Line 90-91)
    await state.mqtt_publish_queue.get()
    await state.mqtt_publish_queue.put(msg)
    mock_client.publish.side_effect = aiomqtt.MqttError("Boom")
    with pytest.raises(aiomqtt.MqttError):
        await mqtt._mqtt_publisher_loop(state, mock_client)
    assert state.mqtt_publish_queue.qsize() == 1


@pytest.mark.asyncio
async def test_mqtt_subscriber_loop_gaps():
    """Cover gaps in _mqtt_subscriber_loop."""
    service = MagicMock()
    mock_client = MagicMock()

    class FakeMessage:
        def __init__(self, topic, payload):
            self.topic = topic
            self.payload = payload

    # Case: payload types (str, int)
    msg_str = FakeMessage("t", "p")
    msg_int = FakeMessage("t", 42)

    # Mock client.messages as an async iterator
    async def fake_messages():
        yield msg_str
        yield msg_int
        raise aiomqtt.MqttError("Boom")

    mock_client.messages = fake_messages()

    with pytest.raises(aiomqtt.MqttError):
        await mqtt._mqtt_subscriber_loop(service, mock_client)


@pytest.mark.asyncio
async def test_mqtt_task_gaps():
    """Cover gaps in mqtt_task."""
    config = create_fake_config()
    state = create_fake_state()
    service = MagicMock()

    # Case: MqttError, OSError in main loop
    with (
        patch("mcubridge.transport.mqtt.aiomqtt.Client", side_effect=OSError("Boom")),
        patch("asyncio.sleep", side_effect=asyncio.CancelledError),
    ):
        with pytest.raises(asyncio.CancelledError):
            await mqtt.mqtt_task(config, state, service)


# --- mcubridge.services.dispatcher ---


@pytest.mark.asyncio
async def test_dispatcher_gaps():
    """Cover remaining gaps in dispatcher.py."""
    mcu_reg = MagicMock()
    mqtt_reg = MagicMock()
    send = AsyncMock()
    ack = AsyncMock()
    is_sync = MagicMock(return_value=True)
    is_allowed = MagicMock(return_value=True)
    reject = AsyncMock()
    snapshot = AsyncMock()

    disp = dispatcher.BridgeDispatcher(mcu_reg, mqtt_reg, send, ack, is_sync, is_allowed, reject, snapshot)

    # _handle_unexpected digital/analog read (pin is None)
    assert await disp._handle_unexpected_digital_read(b"") is False
    assert await disp._handle_unexpected_analog_read(b"") is False

    # With pin component
    disp.pin = MagicMock()
    disp.pin.handle_unexpected_mcu_request = AsyncMock(return_value=True)
    assert await disp._handle_unexpected_digital_read(b"") is True
    assert await disp._handle_unexpected_analog_read(b"") is True

    # dispatch_mcu_frame pre-sync rejection
    is_sync.return_value = False
    with patch("mcubridge.services.dispatcher.logger.warning") as mock_warn:
        await disp.dispatch_mcu_frame(Command.CMD_CONSOLE_WRITE.value, b"test")
        assert mock_warn.called

    # dispatch_mcu_frame handler exception
    is_sync.return_value = True
    mcu_reg.get.return_value = AsyncMock(side_effect=ValueError("Boom"))
    await disp.dispatch_mcu_frame(Command.CMD_CONSOLE_WRITE.value, b"test")
    # Should send Error status back
    assert send.called

    # orphan/unhandled command (no handler registered)
    mcu_reg.get.return_value = None
    # CMD_CONSOLE_WRITE is a request, so it should send NOT_IMPLEMENTED
    await disp.dispatch_mcu_frame(Command.CMD_CONSOLE_WRITE.value, b"test")
    send.assert_called_with(Status.NOT_IMPLEMENTED.value, b"")

    # orphan response (no one waiting)
    # CMD_GET_VERSION_RESP is a response ID
    await disp.dispatch_mcu_frame(Command.CMD_GET_VERSION_RESP.value, b"test")

    # dispatch_mqtt_message route is None
    def parse_none(_):
        return None

    await disp.dispatch_mqtt_message(MagicMock(topic="t"), parse_none)

    # dispatch_mqtt_message empty segments
    def parse_empty(_):
        return TopicRoute("raw", "prefix", Topic.SYSTEM, ())

    await disp.dispatch_mqtt_message(MagicMock(topic="t"), parse_empty)

    # dispatch_mqtt_message exception
    mqtt_reg.dispatch = AsyncMock(side_effect=RuntimeError("Boom"))

    def parse_ok(_):
        return TopicRoute("raw", "prefix", Topic.SYSTEM, ("seg",))

    await disp.dispatch_mqtt_message(MagicMock(topic="t"), parse_ok)

    # Unhandled MQTT topic
    mqtt_reg.dispatch = AsyncMock(return_value=False)
    await disp.dispatch_mqtt_message(MagicMock(topic="t"), parse_ok)

    # _handle_file_topic short segments
    route_short = TopicRoute("raw", "p", Topic.FILE, ("identifier",))
    assert await disp._handle_file_topic(route_short, MagicMock()) is False

    # _handle_file_topic not allowed
    is_allowed.return_value = False
    route_file = TopicRoute("raw", "p", Topic.FILE, ("id", "rem"))
    assert await disp._handle_file_topic(route_file, MagicMock()) is True
    assert reject.called

    # _handle_console_topic identifier != "in"
    route_cons = TopicRoute("raw", "p", Topic.CONSOLE, ("out",))
    assert await disp._handle_console_topic(route_cons, MagicMock()) is False

    # _handle_console_topic not allowed
    is_allowed.return_value = False
    route_cons_in = TopicRoute("raw", "p", Topic.CONSOLE, ("in",))
    assert await disp._handle_console_topic(route_cons_in, MagicMock()) is True

    # _handle_datastore_topic empty identifier
    route_ds_empty = TopicRoute("raw", "p", Topic.DATASTORE, ())
    assert await disp._handle_datastore_topic(route_ds_empty, MagicMock()) is False

    # _handle_datastore_topic not allowed
    route_ds = TopicRoute("raw", "p", Topic.DATASTORE, ("id",))
    is_allowed.return_value = False
    assert await disp._handle_datastore_topic(route_ds, MagicMock()) is True

    # _handle_mailbox_topic not allowed
    route_mb = TopicRoute("raw", "p", Topic.MAILBOX, ("id",))
    is_allowed.return_value = False
    assert await disp._handle_mailbox_topic(route_mb, MagicMock()) is True

    # _handle_shell_topic not allowed
    route_sh = TopicRoute("raw", "p", Topic.SHELL, ("id",))
    is_allowed.return_value = False
    assert await disp._handle_shell_topic(route_sh, MagicMock()) is True

    # _handle_pin_topic not allowed
    is_allowed.return_value = False
    msg_pin = MagicMock(topic="br/digital/13/write", payload=b"1")
    route_pin = TopicRoute("br/digital/13/write", "br", Topic.DIGITAL, ("13", "write"))
    assert await disp._handle_pin_topic(route_pin, msg_pin) is True

    # _handle_system_topic unhandled
    disp.system = MagicMock()
    disp.system.handle_mqtt = AsyncMock(return_value=False)
    route_sys = TopicRoute("raw", "p", Topic.SYSTEM, ("unknown",))
    assert await disp._handle_system_topic(route_sys, MagicMock()) is False

    # _handle_bridge_topic empty remainder
    route_br_empty = TopicRoute("raw", "p", Topic.SYSTEM, ("bridge",))
    assert await disp._handle_bridge_topic(route_br_empty, MagicMock()) is False

    # _handle_bridge_topic handshake get
    route_br_hand = TopicRoute("raw", "p", Topic.SYSTEM, ("bridge", "handshake", "get"))
    assert await disp._handle_bridge_topic(route_br_hand, MagicMock()) is True

    # _handle_bridge_topic summary get
    route_br_summ = TopicRoute("raw", "p", Topic.SYSTEM, ("bridge", "summary", "get"))
    assert await disp._handle_bridge_topic(route_br_summ, MagicMock()) is True

    # _payload_bytes types
    assert disp._payload_bytes(bytearray(b"test")) == b"test"
    assert disp._payload_bytes(memoryview(b"test")) == b"test"
    assert disp._payload_bytes(None) == b""
    assert disp._payload_bytes(123) == b"123"
    with pytest.raises(TypeError):
        disp._payload_bytes({})


# --- mcubridge.state.context ---


def test_context_helpers():
    """Cover helper functions in context.py."""
    # _coerce_snapshot_int branches
    snap = {"a": "10", "b": "invalid", "c": 5.5}
    assert context._coerce_snapshot_int(snap, "a", 0) == 10
    assert context._coerce_snapshot_int(snap, "b", 42) == 42
    assert context._coerce_snapshot_int(snap, "c", 0) == 5
    assert context._coerce_snapshot_int(snap, "missing", 123) == 123

    # _ExponentialBackoff hasattr False
    back = context._ExponentialBackoff(1, 10, 2)
    retry_state = SimpleNamespace()  # no attempt_number
    back(retry_state)
    assert retry_state.attempt_number == 1

    # _status_label unknown
    assert context._status_label(0xFF) == "0xFF"
    assert context._status_label(None) == "unknown"


def test_mcu_capabilities_branches():
    """Cover all capability property branches."""
    # Test with everything enabled
    caps = context.McuCapabilities(features=0xFFFF)
    assert caps.has_watchdog is True
    assert caps.has_rle is True
    assert caps.debug_frames is True
    assert caps.debug_io is True
    assert caps.has_eeprom is True
    assert caps.has_dac is True
    assert caps.has_hw_serial1 is True
    assert caps.has_fpu is True
    assert caps.is_3v3_logic is True
    assert caps.has_large_buffer is True
    assert caps.has_i2c is True

    d = caps.as_dict()
    assert d["has_i2c"] is True


def test_managed_process_gaps():
    """Cover ManagedProcess branches."""
    proc = context.ManagedProcess(pid=123)

    # append_output with empty chunk
    proc.append_output(b"", b"", limit=100)

    # append_output with limit <= 0
    proc.append_output(b"test", b"", limit=0)
    assert len(proc.stdout_buffer) == 4

    # append_output trigger truncation
    proc.append_output(b"chunk", b"", limit=2)
    assert len(proc.stdout_buffer) == 2

    # is_drained
    assert proc.is_drained() is False
    proc.stdout_buffer.clear()
    assert proc.is_drained() is True


def test_latency_stats_gaps():
    """Cover SerialLatencyStats branches."""
    stats = context.SerialLatencyStats()

    # total_observations == 0 path in as_dict
    assert stats.as_dict()["avg_ms"] == 0.0

    # overflow_count branch
    stats.record(10000.0)
    assert stats.overflow_count == 1


@pytest.mark.asyncio
async def test_runtime_state_gaps():
    """Cover remaining gaps in RuntimeState."""
    config = create_fake_config()
    state = context.create_runtime_state(config)

    # enqueue_console_chunk empty
    state.enqueue_console_chunk(b"", MagicMock())

    # enqueue_console_chunk overflow (rejected)
    # Populate the queue first
    state.console_queue_limit_bytes = 10
    state.enqueue_console_chunk(b"1234567890", MagicMock())
    # Now try to append more, triggering dropped_chunks
    state.enqueue_console_chunk(b"extra", MagicMock())
    assert state.console_dropped_chunks > 0

    # trigger not accepted
    state.console_queue_limit_bytes = 0
    state.enqueue_console_chunk(b"fail", MagicMock())

    # requeue_console_chunk_front
    state.requeue_console_chunk_front(b"")  # empty
    state.console_queue_limit_bytes = 5
    state.requeue_console_chunk_front(b"long_chunk")  # truncate

    # enqueue_mailbox_message rejected
    state.mailbox_queue_limit = 0
    assert state.enqueue_mailbox_message(b"test", MagicMock()) is False

    # enqueue_mailbox_incoming rejected
    assert state.enqueue_mailbox_incoming(b"test", MagicMock()) is False

    # record_handshake_failure streak
    state.record_handshake_failure("fail")
    assert state.handshake_failure_streak == 1
    state.record_handshake_failure("fail")
    assert state.handshake_failure_streak == 2
    state.record_handshake_failure("new")
    assert state.handshake_failure_streak == 1

    # record_serial_flow_event invalid
    state.record_serial_flow_event("invalid")

    # record_serial_pipeline_event attempt/timestamp None
    state.record_serial_pipeline_event({"event": "start", "command_id": 1, "attempt": None, "timestamp": None})
    assert state.serial_pipeline_inflight["attempt"] == 1

    # record_serial_pipeline_event inflight None for ack/success
    state.serial_pipeline_inflight = None
    state.record_serial_pipeline_event({"event": "ack"})
    state.record_serial_pipeline_event({"event": "success"})

    # record_serial_pipeline_event started invalid
    state.serial_pipeline_inflight = {"started_unix": "invalid"}
    state.record_serial_pipeline_event({"event": "success"})

    # initialize_spool disabled
    state.mqtt_spool_limit = 0
    state.initialize_spool()

    # ensure_spool disabled/backoff
    assert await state.ensure_spool() is False
    state.mqtt_spool_backoff_until = time.monotonic() + 100
    assert await state.ensure_spool() is False

    # _apply_spool_observation non-int corrupt/last_trim
    state._apply_spool_observation({"corrupt_dropped": "none", "last_trim_unix": "none"})

    # initialize_spool exception
    state.mqtt_spool_dir = "/non/existent/path/that/fails"
    state.mqtt_spool_limit = 100
    with patch("mcubridge.state.context.MQTTPublishSpool", side_effect=OSError("Boom")):
        state.initialize_spool()
        assert state.mqtt_spool_degraded is True


@pytest.mark.asyncio
async def test_runtime_state_spool_operations():
    """Cover spool interaction gaps."""
    config = create_fake_config()
    state = context.create_runtime_state(config)

    # stash_mqtt_message spool is None
    state.mqtt_spool = None
    with patch("mcubridge.state.context.RuntimeState.ensure_spool", new_callable=AsyncMock) as mock_ensure:
        mock_ensure.return_value = False
        assert await state.stash_mqtt_message(QueuedPublish("t", b"p")) is False

    # flush_mqtt_spool spool is None
    state.mqtt_spool = None
    with patch("mcubridge.state.context.RuntimeState.ensure_spool", new_callable=AsyncMock) as mock_ensure:
        mock_ensure.return_value = False
        await state.flush_mqtt_spool()

    # flush_mqtt_spool loop breaks
    state.mqtt_publish_queue = MagicMock()
    state.mqtt_publish_queue.qsize.return_value = 1000  # full
    state.mqtt_queue_limit = 100
    state.mqtt_spool = MagicMock()
    await state.flush_mqtt_spool()

    # flush_mqtt_spool QueueFull requeue
    state.mqtt_publish_queue = asyncio.Queue(1)
    state.mqtt_publish_queue.put_nowait(QueuedPublish("t", b"p"))
    state.mqtt_queue_limit = 10
    state.mqtt_spool = MagicMock()
    state.mqtt_spool.pop_next.return_value = QueuedPublish("t", b"p")
    await state.flush_mqtt_spool()
    assert state.mqtt_spool.requeue.called


# --- mcubridge.mqtt.spool ---


def test_mqtt_spool_gaps():
    """Cover remaining gaps in spool.py."""
    # SqliteDeque popleft IndexError
    with patch("sqlite3.connect"):
        dq = spool.SqliteDeque("/tmp")
        with patch.object(dq, "_conn") as mock_conn:
            mock_conn.execute.return_value.fetchone.return_value = None
            with pytest.raises(IndexError):
                dq.popleft()

    # MQTTSpoolError original is None
    err = spool.MQTTSpoolError("test")
    assert str(err) == "test"

    # MQTTPublishSpool non-tmp directory
    with patch("mcubridge.mqtt.spool.logger.warning") as mock_warn:
        s = spool.MQTTPublishSpool("/home/user", 100)
        assert s.is_degraded
        assert mock_warn.called

    # MQTTPublishSpool initialization failure
    with patch("mcubridge.mqtt.spool.SqliteDeque", side_effect=OSError("Boom")):
        s = spool.MQTTPublishSpool("/tmp/fail", 100)
        assert s.is_degraded

    # close() getattr else branch
    s = spool.MQTTPublishSpool("/tmp/close", 100)
    s._disk_queue = MagicMock(spec=["clear"])
    s.close()
    assert s._disk_queue is None

    # append/requeue except branch
    s = spool.MQTTPublishSpool("/tmp/err", 100)
    s._disk_queue = MagicMock()
    s._disk_queue.append.side_effect = OSError("Boom")
    s.append(QueuedPublish("t", b"p"))
    assert s.is_degraded

    s = spool.MQTTPublishSpool("/tmp/err2", 100)
    s._disk_queue = MagicMock()
    s._disk_queue.appendleft.side_effect = OSError("Boom")
    s.requeue(QueuedPublish("t", b"p"))
    assert s.is_degraded

    # pop_next except branch during disk pop
    s = spool.MQTTPublishSpool("/tmp/poperr", 100)
    s._disk_queue = MagicMock()
    s._disk_queue.__len__.return_value = 1
    s._disk_queue.popleft.side_effect = OSError("Boom")
    assert s.pop_next() is None
    assert s.is_degraded

    # pop_next corrupt entry
    s = spool.MQTTPublishSpool("/tmp/corrupt", 100)
    s._memory_queue.append(MagicMock())  # Not a record
    assert s.pop_next() is None

    # pending except branch
    s = spool.MQTTPublishSpool("/tmp/pend", 100)
    s._disk_queue = MagicMock()
    # Mock __len__ to raise error
    type(s._disk_queue).__len__ = MagicMock(side_effect=OSError("Boom"))
    assert s.pending >= 0

    # _handle_disk_error disk_full branch
    s = spool.MQTTPublishSpool("/tmp/full", 100)
    exc = OSError()
    exc.errno = errno.ENOSPC
    s._handle_disk_error(exc, "test")
    assert s._fallback_active

    # _trim_locked failure branch
    s = spool.MQTTPublishSpool("/tmp/trim", 1)
    s._disk_queue = MagicMock()
    s._disk_queue.__len__.return_value = 10
    s._disk_queue.popleft.side_effect = OSError("Boom")
    s._memory_queue.append(MagicMock())
    s._trim_locked()
    assert s.is_degraded


# --- mcubridge.services.handshake ---


def test_handshake_timing_branches():
    """Cover SerialTimingWindow and derive_serial_timing branches."""
    window = handshake.SerialTimingWindow(100, 500, 5)
    assert window.ack_timeout_seconds == 0.1
    assert window.response_timeout_seconds == 0.5

    cfg = create_fake_config()
    cfg.serial_retry_timeout = 0.001  # Clamp to min
    cfg.serial_response_timeout = 100.0  # Clamp to max
    window2 = handshake.derive_serial_timing(cfg)
    assert window2.ack_timeout_ms >= handshake.protocol.HANDSHAKE_ACK_TIMEOUT_MIN_MS


@pytest.mark.asyncio
async def test_handshake_synchronize_gaps():
    """Cover synchronize logic gaps."""
    config = create_fake_config()
    state = create_fake_state()
    sender = AsyncMock(return_value=False)
    timing = handshake.derive_serial_timing(config)

    comp = handshake.SerialHandshakeManager(
        config=config,
        state=state,
        serial_timing=timing,
        send_frame=sender,
        enqueue_mqtt=AsyncMock(),
        acknowledge_frame=AsyncMock(),
    )

    # reset_ok legacy branch (first RESET fails, second RESET b"" succeeds)
    sender.side_effect = [False, True, True]  # RESET fail, RESET b"" ok, SYNC ok
    with patch.object(comp, "_wait_for_link_sync_confirmation", return_value=True):
        assert await comp.synchronize() is True

    # link_sync_timeout branch pending_nonce != nonce
    sender.side_effect = [True, True]
    with patch.object(comp, "_wait_for_link_sync_confirmation", return_value=False):
        state.link_handshake_nonce = b"someone_else"
        assert await comp.synchronize() is False


@pytest.mark.asyncio
async def test_handshake_handle_resp_gaps():
    """Cover handle_link_sync_resp logic gaps."""
    config = create_fake_config()
    state = create_fake_state()
    comp = handshake.SerialHandshakeManager(
        config=config,
        state=state,
        serial_timing=handshake.derive_serial_timing(config),
        send_frame=AsyncMock(),
        enqueue_mqtt=AsyncMock(),
        acknowledge_frame=AsyncMock(),
    )

    # rate_limit branch
    state.link_handshake_nonce = b"n" * 16
    state.handshake_rate_limit_until = time.monotonic() + 100
    assert await comp.handle_link_sync_resp(b"payload") is False

    # malformed length branch
    state.handshake_rate_limit_until = 0
    assert await comp.handle_link_sync_resp(b"too_short") is False

    # replay detected branch
    state.link_handshake_nonce = b"n" * 16
    state.link_nonce_length = 16
    state.link_expected_tag = b"t" * 16
    state.link_last_nonce_counter = 100
    # Correct length payload: nonce(16) + tag(16) = 32
    # Nonce counter is in last 8 bytes.
    # Let's craft a replay nonce (counter <= 100)
    nonce = b"r" * 8 + struct.pack(">Q", 50)
    state.link_handshake_nonce = nonce
    tag = comp._compute_handshake_tag(nonce)
    assert await comp.handle_link_sync_resp(nonce + tag) is False


@pytest.mark.asyncio
async def test_handshake_other_gaps():
    """Cover miscellaneous handshake gaps."""
    comp = handshake.SerialHandshakeManager(
        config=create_fake_config(),
        state=create_fake_state(),
        serial_timing=MagicMock(),
        send_frame=AsyncMock(),
        enqueue_mqtt=AsyncMock(),
        acknowledge_frame=AsyncMock(),
    )

    # raise_if_handshake_fatal reason False
    comp._state.handshake_fatal_reason = None
    comp.raise_if_handshake_fatal()  # No exception

    # _maybe_schedule_handshake_backoff streak < threshold
    comp._state.handshake_failure_streak = 1
    assert comp._maybe_schedule_handshake_backoff("io_error") is None

    # _should_mark_failure_fatal _is_immediate_fatal True
    assert comp._should_mark_failure_fatal("sync_auth_mismatch") is True

    # retryer exception in _fetch_capabilities
    with (
        patch("asyncio.get_running_loop"),
        patch("tenacity.AsyncRetrying.__aiter__", side_effect=tenacity.RetryError(MagicMock())),
    ):
        assert await comp._fetch_capabilities() is False


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
        config=config, state=state, serial_timing=timing, send_frame=sender, enqueue_mqtt=enqueue, acknowledge_frame=ack
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
        config=config, state=state, serial_timing=timing, send_frame=sender, enqueue_mqtt=enqueue, acknowledge_frame=ack
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
        config=config, state=state, serial_timing=timing, send_frame=sender, enqueue_mqtt=enqueue, acknowledge_frame=ack
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
        config=config, state=state, serial_timing=timing, send_frame=sender, enqueue_mqtt=enqueue, acknowledge_frame=ack
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
        config=config, state=state, serial_timing=timing, send_frame=sender, enqueue_mqtt=enqueue, acknowledge_frame=ack
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
        acknowledge_frame=AsyncMock(),
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
        patch("mcubridge.hasattr", return_value=False),
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
