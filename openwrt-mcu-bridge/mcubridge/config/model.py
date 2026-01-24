"""Data model for MCU Bridge configuration."""

from __future__ import annotations

from dataclasses import dataclass, field

from ..const import (
    DEFAULT_ALLOW_NON_TMP_PATHS,
    DEFAULT_BRIDGE_HANDSHAKE_INTERVAL,
    DEFAULT_BRIDGE_SUMMARY_INTERVAL,
    DEFAULT_CONSOLE_QUEUE_LIMIT_BYTES,
    DEFAULT_DEBUG_LOGGING,
    DEFAULT_FILE_STORAGE_QUOTA_BYTES,
    DEFAULT_FILE_SYSTEM_ROOT,
    DEFAULT_FILE_WRITE_MAX_BYTES,
    DEFAULT_MAILBOX_QUEUE_BYTES_LIMIT,
    DEFAULT_MAILBOX_QUEUE_LIMIT,
    DEFAULT_METRICS_ENABLED,
    DEFAULT_METRICS_HOST,
    DEFAULT_METRICS_PORT,
    DEFAULT_MQTT_CAFILE,
    DEFAULT_MQTT_HOST,
    DEFAULT_MQTT_PORT,
    DEFAULT_MQTT_QUEUE_LIMIT,
    DEFAULT_MQTT_SPOOL_DIR,
    DEFAULT_MQTT_TLS_INSECURE,
    DEFAULT_PENDING_PIN_REQUESTS,
    DEFAULT_PROCESS_MAX_CONCURRENT,
    DEFAULT_PROCESS_MAX_OUTPUT_BYTES,
    DEFAULT_PROCESS_TIMEOUT,
    DEFAULT_RECONNECT_DELAY,
    DEFAULT_SERIAL_HANDSHAKE_FATAL_FAILURES,
    DEFAULT_SERIAL_HANDSHAKE_MIN_INTERVAL,
    DEFAULT_SERIAL_RESPONSE_TIMEOUT,
    DEFAULT_SERIAL_RETRY_TIMEOUT,
    DEFAULT_STATUS_INTERVAL,
    DEFAULT_WATCHDOG_ENABLED,
    DEFAULT_WATCHDOG_INTERVAL,
)
from ..policy import AllowedCommandPolicy, TopicAuthorization
from ..rpc.protocol import DEFAULT_RETRY_LIMIT


@dataclass(slots=True)
class RuntimeConfig:
    """Strongly typed configuration for the daemon."""

    serial_port: str
    serial_baud: int
    serial_safe_baud: int
    mqtt_host: str
    mqtt_port: int
    mqtt_user: str | None
    mqtt_pass: str | None
    mqtt_tls: bool
    mqtt_cafile: str | None
    mqtt_certfile: str | None
    mqtt_keyfile: str | None
    mqtt_topic: str
    allowed_commands: tuple[str, ...]
    file_system_root: str
    process_timeout: int
    mqtt_tls_insecure: bool = DEFAULT_MQTT_TLS_INSECURE
    file_write_max_bytes: int = DEFAULT_FILE_WRITE_MAX_BYTES
    file_storage_quota_bytes: int = DEFAULT_FILE_STORAGE_QUOTA_BYTES
    allowed_policy: AllowedCommandPolicy = field(init=False)

    mqtt_queue_limit: int = DEFAULT_MQTT_QUEUE_LIMIT
    reconnect_delay: int = DEFAULT_RECONNECT_DELAY
    status_interval: int = DEFAULT_STATUS_INTERVAL
    debug_logging: bool = DEFAULT_DEBUG_LOGGING
    console_queue_limit_bytes: int = DEFAULT_CONSOLE_QUEUE_LIMIT_BYTES
    mailbox_queue_limit: int = DEFAULT_MAILBOX_QUEUE_LIMIT
    mailbox_queue_bytes_limit: int = DEFAULT_MAILBOX_QUEUE_BYTES_LIMIT
    pending_pin_request_limit: int = DEFAULT_PENDING_PIN_REQUESTS
    serial_retry_timeout: float = DEFAULT_SERIAL_RETRY_TIMEOUT
    serial_response_timeout: float = DEFAULT_SERIAL_RESPONSE_TIMEOUT
    serial_retry_attempts: int = DEFAULT_RETRY_LIMIT
    serial_handshake_min_interval: float = DEFAULT_SERIAL_HANDSHAKE_MIN_INTERVAL
    serial_handshake_fatal_failures: int = DEFAULT_SERIAL_HANDSHAKE_FATAL_FAILURES
    watchdog_enabled: bool = DEFAULT_WATCHDOG_ENABLED
    watchdog_interval: float = DEFAULT_WATCHDOG_INTERVAL
    topic_authorization: TopicAuthorization = field(default_factory=TopicAuthorization)
    serial_shared_secret: bytes = field(repr=False, default=b"")
    mqtt_spool_dir: str = DEFAULT_MQTT_SPOOL_DIR
    process_max_output_bytes: int = DEFAULT_PROCESS_MAX_OUTPUT_BYTES
    process_max_concurrent: int = DEFAULT_PROCESS_MAX_CONCURRENT
    metrics_enabled: bool = DEFAULT_METRICS_ENABLED
    metrics_host: str = DEFAULT_METRICS_HOST
    metrics_port: int = DEFAULT_METRICS_PORT
    bridge_summary_interval: float = DEFAULT_BRIDGE_SUMMARY_INTERVAL
    bridge_handshake_interval: float = DEFAULT_BRIDGE_HANDSHAKE_INTERVAL
    allow_non_tmp_paths: bool = DEFAULT_ALLOW_NON_TMP_PATHS

    @property
    def tls_enabled(self) -> bool:
        return self.mqtt_tls

    def __post_init__(self) -> None:
        # Policy is derived from allowed_commands, difficult to validate in Schema
        # without custom field. We'll keep this derivation here but assume
        # allowed_commands is already validated list of strings.
        self.allowed_policy = AllowedCommandPolicy.from_iterable(self.allowed_commands)
