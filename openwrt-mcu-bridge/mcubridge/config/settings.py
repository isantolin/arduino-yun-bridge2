"""Settings loader for the MCU Bridge daemon.

Configuration is loaded from OpenWrt UCI (package `mcubridge`, section
`general`) with sane defaults for non-OpenWrt environments.

Runtime configuration is intentionally **UCI-only**: environment variables are
not used as overrides.
"""

from __future__ import annotations

import logging
import os
import sys
from pathlib import Path
from typing import Any

# [SIL-2] Deterministic Import: msgspec is MANDATORY.
import msgspec

from ..config.common import (
    get_default_config,
    get_uci_config,
    normalise_allowed_commands,
    parse_bool,
)
from ..config.const import (
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
    DEFAULT_SERIAL_PORT,
    DEFAULT_SERIAL_RESPONSE_TIMEOUT,
    DEFAULT_SERIAL_RETRY_TIMEOUT,
    DEFAULT_SERIAL_SHARED_SECRET,
    DEFAULT_STATUS_INTERVAL,
    DEFAULT_WATCHDOG_INTERVAL,
    MIN_SERIAL_SHARED_SECRET_LEN,
    VOLATILE_STORAGE_PATHS,
)
from ..policy import AllowedCommandPolicy, TopicAuthorization
from ..protocol.protocol import (
    DEFAULT_BAUDRATE,
    DEFAULT_RETRY_LIMIT,
    DEFAULT_SAFE_BAUDRATE,
    MQTT_DEFAULT_TOPIC_PREFIX,
)

logger = logging.getLogger(__name__)


class RuntimeConfig(msgspec.Struct, kw_only=True):
    """Strongly typed configuration for the daemon."""

    serial_port: str = DEFAULT_SERIAL_PORT
    serial_baud: int = msgspec.field(default=DEFAULT_BAUDRATE, ge=300)
    serial_safe_baud: int = msgspec.field(default=DEFAULT_SAFE_BAUDRATE, ge=300)
    mqtt_host: str = DEFAULT_MQTT_HOST
    mqtt_port: int = msgspec.field(default=DEFAULT_MQTT_PORT, ge=1, le=65535)
    mqtt_user: str | None = None
    mqtt_pass: str | None = None
    mqtt_tls: bool = True
    mqtt_cafile: str | None = DEFAULT_MQTT_CAFILE
    mqtt_certfile: str | None = None
    mqtt_keyfile: str | None = None
    mqtt_topic: str = MQTT_DEFAULT_TOPIC_PREFIX
    allowed_commands: tuple[str, ...] = ()
    file_system_root: str = DEFAULT_FILE_SYSTEM_ROOT
    process_timeout: int = msgspec.field(default=DEFAULT_PROCESS_TIMEOUT, ge=1)

    mqtt_tls_insecure: bool = DEFAULT_MQTT_TLS_INSECURE
    file_write_max_bytes: int = msgspec.field(default=DEFAULT_FILE_WRITE_MAX_BYTES, ge=1)
    file_storage_quota_bytes: int = msgspec.field(default=DEFAULT_FILE_STORAGE_QUOTA_BYTES, ge=1)

    allowed_policy: AllowedCommandPolicy | None = None

    mqtt_queue_limit: int = msgspec.field(default=DEFAULT_MQTT_QUEUE_LIMIT, ge=0)
    reconnect_delay: int = msgspec.field(default=DEFAULT_RECONNECT_DELAY, ge=1)
    status_interval: int = msgspec.field(default=DEFAULT_STATUS_INTERVAL, ge=1)
    debug_logging: bool = DEFAULT_DEBUG_LOGGING
    console_queue_limit_bytes: int = msgspec.field(default=DEFAULT_CONSOLE_QUEUE_LIMIT_BYTES, ge=1)
    mailbox_queue_limit: int = msgspec.field(default=DEFAULT_MAILBOX_QUEUE_LIMIT, ge=1)
    mailbox_queue_bytes_limit: int = msgspec.field(default=DEFAULT_MAILBOX_QUEUE_BYTES_LIMIT, ge=1)
    pending_pin_request_limit: int = msgspec.field(default=DEFAULT_PENDING_PIN_REQUESTS, ge=1)
    serial_retry_timeout: float = msgspec.field(default=DEFAULT_SERIAL_RETRY_TIMEOUT, ge=0.01)
    serial_response_timeout: float = msgspec.field(default=DEFAULT_SERIAL_RESPONSE_TIMEOUT, ge=0.02)
    serial_retry_attempts: int = msgspec.field(default=DEFAULT_RETRY_LIMIT, ge=0)
    serial_handshake_min_interval: float = msgspec.field(default=DEFAULT_SERIAL_HANDSHAKE_MIN_INTERVAL, ge=0.0)
    serial_handshake_fatal_failures: int = msgspec.field(default=DEFAULT_SERIAL_HANDSHAKE_FATAL_FAILURES, ge=1)
    watchdog_enabled: bool = True
    watchdog_interval: float = msgspec.field(default=DEFAULT_WATCHDOG_INTERVAL, ge=0.5)
    topic_authorization: TopicAuthorization = TopicAuthorization()

    # msgspec handle bytes naturally.
    # [SIL-2] SECURITY: This default enables initial setup only.
    # It MUST be rotated using 'mcubridge-rotate-credentials'.
    serial_shared_secret: bytes = DEFAULT_SERIAL_SHARED_SECRET

    mqtt_spool_dir: str = DEFAULT_MQTT_SPOOL_DIR
    process_max_output_bytes: int = msgspec.field(default=DEFAULT_PROCESS_MAX_OUTPUT_BYTES, ge=1)
    process_max_concurrent: int = msgspec.field(default=DEFAULT_PROCESS_MAX_CONCURRENT, ge=1)
    metrics_enabled: bool = DEFAULT_METRICS_ENABLED
    metrics_host: str = DEFAULT_METRICS_HOST
    metrics_port: int = msgspec.field(default=DEFAULT_METRICS_PORT, ge=1, le=65535)
    bridge_summary_interval: float = msgspec.field(default=DEFAULT_BRIDGE_SUMMARY_INTERVAL, ge=0.0)
    bridge_handshake_interval: float = msgspec.field(default=DEFAULT_BRIDGE_HANDSHAKE_INTERVAL, ge=0.0)
    allow_non_tmp_paths: bool = DEFAULT_ALLOW_NON_TMP_PATHS

    @property
    def tls_enabled(self) -> bool:
        return self.mqtt_tls

    def __post_init__(self) -> None:
        # Normalize optional strings to None if empty
        self.mqtt_user = self._normalize_optional_string(self.mqtt_user)
        self.mqtt_pass = self._normalize_optional_string(self.mqtt_pass)
        self.mqtt_cafile = self._normalize_optional_string(self.mqtt_cafile)
        self.mqtt_certfile = self._normalize_optional_string(self.mqtt_certfile)
        self.mqtt_keyfile = self._normalize_optional_string(self.mqtt_keyfile)

        self.allowed_policy = AllowedCommandPolicy.from_iterable(self.allowed_commands)
        # Runtime derived validations
        self.serial_response_timeout = max(self.serial_response_timeout, self.serial_retry_timeout * 2)

        if not self.mqtt_tls:
            logger.warning("MQTT TLS is disabled; MQTT credentials and payloads will be sent in plaintext.")
        else:
            if self.mqtt_tls_insecure:
                logger.warning(
                    "MQTT TLS hostname verification is disabled (mqtt_tls_insecure=1); "
                    "this is less secure and should be used only for known/self-hosted brokers."
                )
            if not self.mqtt_cafile:
                logger.info("MQTT TLS is enabled with no mqtt_cafile configured; using system trust store.")
        
        if not self.serial_shared_secret:
            raise ValueError("serial_shared_secret must be configured")
        if len(self.serial_shared_secret) < MIN_SERIAL_SHARED_SECRET_LEN:
            raise ValueError("serial_shared_secret must be at least %d bytes" % MIN_SERIAL_SHARED_SECRET_LEN)
        if self.serial_shared_secret == b"changeme123":
            raise ValueError("serial_shared_secret placeholder is insecure")
        
        unique_symbols = {byte for byte in self.serial_shared_secret}
        if len(unique_symbols) < 4:
            raise ValueError("serial_shared_secret must contain at least four distinct bytes")
        
        self._normalize_topic_prefix()

        self.file_system_root = os.path.abspath(self.file_system_root)
        self.mqtt_spool_dir = os.path.abspath(self.mqtt_spool_dir)

        # Logic-based cross-field validations
        if self.file_storage_quota_bytes < self.file_write_max_bytes:
            raise ValueError("file_storage_quota_bytes must be greater than or equal to file_write_max_bytes")

        if self.mailbox_queue_bytes_limit < self.mailbox_queue_limit:
            raise ValueError("mailbox_queue_bytes_limit must be greater than or equal to mailbox_queue_limit")

        # [SIL-2] Flash Protection: Spooling must ALWAYS be in volatile RAM.
        if not any(self.mqtt_spool_dir.startswith(p) for p in VOLATILE_STORAGE_PATHS):
            raise ValueError("FLASH PROTECTION: mqtt_spool_dir must be in a volatile location")

        if not self.allow_non_tmp_paths:
            if not any(self.file_system_root.startswith(p) for p in VOLATILE_STORAGE_PATHS):
                raise ValueError("FLASH PROTECTION: file_system_root must be in a volatile location")

    @staticmethod
    def _normalize_optional_string(value: str | None) -> str | None:
        if not value:
            return None
        s = value.strip()
        return s if s else None

    def _normalize_topic_prefix(self) -> None:
        normalized = self._build_topic_prefix(self.mqtt_topic)
        self.mqtt_topic = normalized

    @staticmethod
    def _build_topic_prefix(prefix: str) -> str:
        segments = [segment.strip() for segment in prefix.split("/") if segment.strip()]
        normalized = "/".join(segments)
        if not normalized:
            raise ValueError("mqtt_topic must contain at least one segment")
        return normalized

    @staticmethod
    def _normalize_path(
        value: str,
        *,
        field_name: str,
        require_absolute: bool,
    ) -> str:
        candidate = (value or "").strip()
        if not candidate:
            raise ValueError(f"{field_name} must be a non-empty path")
        expanded = Path(candidate).expanduser()
        normalized = str(expanded.resolve())
        if require_absolute and not expanded.is_absolute():
            raise ValueError(f"{field_name} must be an absolute path")
        return normalized


def _load_raw_config() -> tuple[dict[str, Any], str]:
    """Load configuration from UCI with robust error handling (SIL 2).

    Returns:
        Tuple of (config_dict, source) where source is 'uci' or 'defaults'.
    """
    try:
        uci_values = get_uci_config()
        if uci_values:
            return uci_values, "uci"
    except (OSError, ValueError) as err:
        # [SIL-2] Catch specific errors to differentiate operational issues from bugs.
        # Fallback to defaults is acceptable here to ensure Fail-Operational behavior.
        logger.error("Failed to load UCI configuration (Operational Error): %s", err)

    logger.warning("Using default configuration (UCI unavailable)")
    return get_default_config(), "defaults"


# Module-level variable to track config source for observability
class ConfigState:
    source: str = "uci"


_CONFIG_STATE = ConfigState()


def get_config_source() -> str:
    """Return the source of the last loaded configuration ('uci' or 'defaults')."""
    return _CONFIG_STATE.source


def load_runtime_config() -> RuntimeConfig:
    """Load configuration from UCI/defaults using msgspec for efficient validation."""

    raw_config, source = _load_raw_config()
    _CONFIG_STATE.source = source

    # Pre-process 'allowed_commands' since msgspec handles standard types
    if "allowed_commands" in raw_config:
        allowed_raw = raw_config["allowed_commands"]
        if isinstance(allowed_raw, str):
            commands = normalise_allowed_commands(allowed_raw.split())
            raw_config["allowed_commands"] = commands

    # Map 'debug' (from UCI) to 'debug_logging' (internal).
    if "debug" in raw_config:
        raw_config["debug_logging"] = parse_bool(raw_config.pop("debug"))

    # Pre-process 'serial_shared_secret'
    if "serial_shared_secret" in raw_config:
        secret = raw_config["serial_shared_secret"]
        if isinstance(secret, str):
            raw_config["serial_shared_secret"] = secret.strip().encode("utf-8")

    # Normalization (Explicit path expansion)
    if "file_system_root" in raw_config:
        raw_config["file_system_root"] = os.path.abspath(os.path.expanduser(raw_config["file_system_root"]))
    if "mqtt_spool_dir" in raw_config:
        raw_config["mqtt_spool_dir"] = os.path.abspath(os.path.expanduser(raw_config["mqtt_spool_dir"]))
    if "mqtt_topic" in raw_config:
        # Handle case where mqtt_topic might be a string that needs splitting or just sanitization
        if isinstance(raw_config["mqtt_topic"], str):
            raw_config["mqtt_topic"] = "/".join(s.strip() for s in raw_config["mqtt_topic"].split("/") if s.strip())

    try:
        config = msgspec.convert(raw_config, RuntimeConfig, strict=False)

        # Validation Logic (moved from __post_init__ or explicit check)
        # Note: We rely on __post_init__ for most checks, but we can verify critical ones
        # here or call post_init explicitly.
        # msgspec calls __post_init__ automatically.

        return config
    except (msgspec.ValidationError, TypeError, ValueError) as e:
        if "pytest" in sys.modules and source == "test":
            raise
        logger.critical("Configuration validation failed: %s", e)
        logger.warning("Falling back to safe defaults due to validation error.")
        return msgspec.convert(get_default_config(), RuntimeConfig, strict=False)
