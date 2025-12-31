import pytest
from unittest.mock import MagicMock

from yunbridge.config.settings import RuntimeConfig
from yunbridge.const import (
    DEFAULT_BRIDGE_HANDSHAKE_INTERVAL,
    DEFAULT_BRIDGE_SUMMARY_INTERVAL,
    DEFAULT_CONSOLE_QUEUE_LIMIT_BYTES,
    DEFAULT_FILE_STORAGE_QUOTA_BYTES,
    DEFAULT_FILE_WRITE_MAX_BYTES,
    DEFAULT_MAILBOX_QUEUE_BYTES_LIMIT,
    DEFAULT_MAILBOX_QUEUE_LIMIT,
    DEFAULT_METRICS_HOST,
    DEFAULT_METRICS_PORT,
    DEFAULT_MQTT_PORT,
    DEFAULT_MQTT_TOPIC,
    DEFAULT_PENDING_PIN_REQUESTS,
    DEFAULT_PROCESS_MAX_CONCURRENT,
    DEFAULT_PROCESS_MAX_OUTPUT_BYTES,
    DEFAULT_PROCESS_TIMEOUT,
    DEFAULT_RECONNECT_DELAY,
    DEFAULT_SERIAL_HANDSHAKE_FATAL_FAILURES,
    DEFAULT_SERIAL_HANDSHAKE_MIN_INTERVAL,
    DEFAULT_SERIAL_RESPONSE_TIMEOUT,
    DEFAULT_STATUS_INTERVAL,
    DEFAULT_WATCHDOG_INTERVAL,
    SUPERVISOR_DEFAULT_MAX_BACKOFF,
    SUPERVISOR_DEFAULT_MIN_BACKOFF,
    SUPERVISOR_DEFAULT_RESTART_INTERVAL,
)

# Mock values for tests
TEST_SERIAL_PORT = "/dev/null"
TEST_SERIAL_BAUD = 115200
TEST_SERIAL_SAFE_BAUD = 115200
TEST_MQTT_HOST = "localhost"
TEST_MQTT_CAFILE = "/tmp/test-ca.pem"
TEST_FILE_SYSTEM_ROOT = "/tmp"
TEST_SERIAL_SHARED_SECRET = b"unit-test-secret-1234"
TEST_MQTT_SPOOL_DIR = "/tmp/yunbridge-tests-spool"


@pytest.fixture()
def runtime_config() -> RuntimeConfig:
    return RuntimeConfig(
        serial_port=TEST_SERIAL_PORT,
        serial_baud=TEST_SERIAL_BAUD,
        serial_safe_baud=TEST_SERIAL_SAFE_BAUD,
        serial_shared_secret=TEST_SERIAL_SHARED_SECRET,
        serial_retry_timeout=0.01,
        serial_response_timeout=DEFAULT_SERIAL_RESPONSE_TIMEOUT,
        serial_retry_attempts=1,
        serial_handshake_min_interval=DEFAULT_SERIAL_HANDSHAKE_MIN_INTERVAL,
        serial_handshake_fatal_failures=DEFAULT_SERIAL_HANDSHAKE_FATAL_FAILURES,
        mqtt_host=TEST_MQTT_HOST,
        mqtt_port=DEFAULT_MQTT_PORT,
        mqtt_tls=True,
        mqtt_cafile=TEST_MQTT_CAFILE,
        mqtt_certfile=None,
        mqtt_keyfile=None,
        mqtt_user=None,
        mqtt_pass=None,
        mqtt_topic=DEFAULT_MQTT_TOPIC,
        mqtt_spool_dir=TEST_MQTT_SPOOL_DIR,
        mqtt_queue_limit=8,
        file_system_root=TEST_FILE_SYSTEM_ROOT,
        file_write_max_bytes=DEFAULT_FILE_WRITE_MAX_BYTES,
        file_storage_quota_bytes=DEFAULT_FILE_STORAGE_QUOTA_BYTES,
        process_timeout=DEFAULT_PROCESS_TIMEOUT,
        process_max_output_bytes=DEFAULT_PROCESS_MAX_OUTPUT_BYTES,
        process_max_concurrent=DEFAULT_PROCESS_MAX_CONCURRENT,
        console_queue_limit_bytes=DEFAULT_CONSOLE_QUEUE_LIMIT_BYTES,
        mailbox_queue_limit=DEFAULT_MAILBOX_QUEUE_LIMIT,
        mailbox_queue_bytes_limit=DEFAULT_MAILBOX_QUEUE_BYTES_LIMIT,
        pending_pin_request_limit=DEFAULT_PENDING_PIN_REQUESTS,
        reconnect_delay=DEFAULT_RECONNECT_DELAY,
        status_interval=DEFAULT_STATUS_INTERVAL,
        bridge_summary_interval=DEFAULT_BRIDGE_SUMMARY_INTERVAL,
        bridge_handshake_interval=DEFAULT_BRIDGE_HANDSHAKE_INTERVAL,
        debug_logging=False,
        allowed_commands=(),
        metrics_enabled=False,
        metrics_host=DEFAULT_METRICS_HOST,
        metrics_port=DEFAULT_METRICS_PORT,
        watchdog_enabled=False,
        watchdog_interval=DEFAULT_WATCHDOG_INTERVAL,
        supervisor_restart_interval=SUPERVISOR_DEFAULT_RESTART_INTERVAL,
        supervisor_min_backoff=SUPERVISOR_DEFAULT_MIN_BACKOFF,
        supervisor_max_backoff=SUPERVISOR_DEFAULT_MAX_BACKOFF,
    )


@pytest.fixture
def mock_serial_port():
    mock = MagicMock()
    mock.port = TEST_SERIAL_PORT
    mock.baudrate = TEST_SERIAL_BAUD
    mock.is_open = True
    return mock


@pytest.fixture
def mock_mqtt_client():
    mock = MagicMock()
    return mock
