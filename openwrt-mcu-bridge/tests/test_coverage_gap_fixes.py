"""Tests to close coverage gaps in the mcubridge ecosystem."""

import logging
import asyncio
from unittest.mock import MagicMock, patch, AsyncMock
from types import SimpleNamespace
import pytest
import mcubridge.common as common
import mcubridge.metrics as metrics
import mcubridge.daemon as daemon
from mcubridge.services.handshake import derive_serial_timing


# --- Helpers ---

def create_fake_config():
    """Create a real namespace config object to avoid MagicMock comparison issues."""
    return SimpleNamespace(
        serial_port="/dev/ttyFake",
        serial_baud=115200,
        mqtt_host="localhost",
        mqtt_port=1883,
        mqtt_tls=False,
        mqtt_tls_insecure=False,
        mqtt_cafile="",
        mqtt_certfile="",
        mqtt_keyfile="",
        mqtt_user="",
        mqtt_pass="",
        mqtt_topic="br",
        mqtt_spool_dir="/tmp/spool",
        mqtt_queue_limit=100,
        serial_shared_secret="secret_1234567890123456",
        serial_retry_timeout=1.0,
        serial_response_timeout=5.0,
        serial_retry_attempts=5,
        serial_handshake_min_interval=1.0,
        serial_handshake_fatal_failures=10,
        debug=False,
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


# --- mcubridge.common ---

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

    # Test with content_type only
    msg = QueuedPublish(topic_name="t", payload=b"", content_type="text/plain")
    props = common.build_mqtt_properties(msg)
    assert props.ContentType == "text/plain"

    # Test with payload_format_indicator
    msg = QueuedPublish(topic_name="t", payload=b"", payload_format_indicator=1)
    props = common.build_mqtt_properties(msg)
    assert props.PayloadFormatIndicator == 1

    # Test with response_topic
    msg = QueuedPublish(topic_name="t", payload=b"", response_topic="resp")
    props = common.build_mqtt_properties(msg)
    assert props.ResponseTopic == "resp"

    # Test with correlation_data
    msg = QueuedPublish(topic_name="t", payload=b"", correlation_data=b"corr")
    props = common.build_mqtt_properties(msg)
    assert props.CorrelationData == b"corr"


@patch("mcubridge.common.os.path.exists")
@patch("mcubridge.common.uci.Uci")
def test_get_uci_config_openwrt_failures(mock_uci_class, mock_exists):
    """Cover UCI failure paths on OpenWrt."""
    mock_exists.return_value = True
    mock_cursor = MagicMock()
    mock_uci_class.return_value.__enter__.return_value = mock_cursor

    # Ensure we use the exact exception class from the module
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

    # Case 3: OSError -> RuntimeError
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
    with patch("asyncio.TaskGroup.__aenter__", side_effect=RuntimeError("Generic failure")):
        # On Python 3.11+, TaskGroup raises ExceptionGroup
        with pytest.raises((RuntimeError, ExceptionGroup)):
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
