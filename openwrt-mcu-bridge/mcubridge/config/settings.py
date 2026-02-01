"""Settings loader for the MCU Bridge daemon.

Configuration is loaded from OpenWrt UCI (package `mcubridge`, section
`general`) with sane defaults for non-OpenWrt environments.

Runtime configuration is intentionally **UCI-only**: environment variables are
not used as overrides.
"""

from __future__ import annotations

import logging
from pathlib import Path
# [SIL-2] Deterministic Import: msgspec is MANDATORY.
import msgspec
from typing import Any


from ..common import (
    get_default_config,
    get_uci_config,
    normalise_allowed_commands,
    parse_bool,
)
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
    DEFAULT_MQTT_QUEUE_LIMIT,
    DEFAULT_MQTT_SPOOL_DIR,
    DEFAULT_MQTT_TLS_INSECURE,
    DEFAULT_PENDING_PIN_REQUESTS,
    DEFAULT_PROCESS_MAX_CONCURRENT,
    DEFAULT_PROCESS_MAX_OUTPUT_BYTES,
    DEFAULT_RECONNECT_DELAY,
    DEFAULT_SERIAL_HANDSHAKE_FATAL_FAILURES,
    DEFAULT_SERIAL_HANDSHAKE_MIN_INTERVAL,
    DEFAULT_SERIAL_PORT,
    DEFAULT_SERIAL_RESPONSE_TIMEOUT,
    DEFAULT_SERIAL_RETRY_TIMEOUT,
    DEFAULT_STATUS_INTERVAL,
    DEFAULT_WATCHDOG_ENABLED,
    DEFAULT_WATCHDOG_INTERVAL,
    MIN_SERIAL_SHARED_SECRET_LEN,
)
from ..policy import AllowedCommandPolicy, TopicAuthorization
from ..rpc.protocol import DEFAULT_RETRY_LIMIT, DEFAULT_BAUDRATE, DEFAULT_SAFE_BAUDRATE


logger = logging.getLogger(__name__)


class RuntimeConfig(msgspec.Struct, kw_only=True):
    """Strongly typed configuration for the daemon."""

    serial_port: str = DEFAULT_SERIAL_PORT
    serial_baud: int = DEFAULT_BAUDRATE
    serial_safe_baud: int = DEFAULT_SAFE_BAUDRATE
    mqtt_host: str = DEFAULT_METRICS_HOST
    mqtt_port: int = DEFAULT_METRICS_PORT
    mqtt_user: str | None = None
    mqtt_pass: str | None = None
    mqtt_tls: bool = True
    mqtt_cafile: str | None = None
    mqtt_certfile: str | None = None
    mqtt_keyfile: str | None = None
    mqtt_topic: str = "br"
    allowed_commands: tuple[str, ...] = ()
    file_system_root: str = DEFAULT_FILE_SYSTEM_ROOT
    process_timeout: int = 30

    mqtt_tls_insecure: bool = DEFAULT_MQTT_TLS_INSECURE
    file_write_max_bytes: int = DEFAULT_FILE_WRITE_MAX_BYTES
    file_storage_quota_bytes: int = DEFAULT_FILE_STORAGE_QUOTA_BYTES

    allowed_policy: AllowedCommandPolicy | None = None

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
    topic_authorization: TopicAuthorization = TopicAuthorization()

    # msgspec handle bytes naturally.
    serial_shared_secret: bytes = b""

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
        # Normalize optional strings to None if empty
        self.mqtt_user = self._normalize_optional_string(self.mqtt_user)
        self.mqtt_pass = self._normalize_optional_string(self.mqtt_pass)
        self.mqtt_cafile = self._normalize_optional_string(self.mqtt_cafile)
        self.mqtt_certfile = self._normalize_optional_string(self.mqtt_certfile)
        self.mqtt_keyfile = self._normalize_optional_string(self.mqtt_keyfile)

        self.allowed_policy = AllowedCommandPolicy.from_iterable(self.allowed_commands)
        self.serial_response_timeout = max(self.serial_response_timeout, self.serial_retry_timeout * 2)
        self.serial_handshake_min_interval = max(0.0, self.serial_handshake_min_interval)
        self.serial_handshake_fatal_failures = self._require_positive(
            "serial_handshake_fatal_failures",
            int(self.serial_handshake_fatal_failures),
        )
        if not self.mqtt_tls:
            logger.warning("MQTT TLS is disabled; MQTT credentials and payloads " "will be sent in plaintext.")
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
        self.pending_pin_request_limit = max(1, self.pending_pin_request_limit)
        unique_symbols = {byte for byte in self.serial_shared_secret}
        if len(unique_symbols) < 4:
            raise ValueError("serial_shared_secret must contain at least " "four distinct bytes")
        self._validate_queue_limits()
        self._normalize_topic_prefix()
        self._validate_flash_protection()
        self._validate_operational_limits()

    @staticmethod
    def _normalize_optional_string(value: str | None) -> str | None:
        if not value:
            return None
        s = value.strip()
        return s if s else None

    def _validate_queue_limits(self) -> None:
        mailbox_limit = self._require_positive(
            "mailbox_queue_limit",
            self.mailbox_queue_limit,
        )
        mailbox_bytes_limit = self._require_positive(
            "mailbox_queue_bytes_limit",
            self.mailbox_queue_bytes_limit,
        )
        if mailbox_bytes_limit < mailbox_limit:
            raise ValueError("mailbox_queue_bytes_limit must be greater than or equal to " "mailbox_queue_limit")
        console_limit = self._require_positive(
            "console_queue_limit_bytes",
            self.console_queue_limit_bytes,
        )
        mqtt_limit = self._require_positive(
            "mqtt_queue_limit",
            self.mqtt_queue_limit,
        )
        self.mailbox_queue_limit = mailbox_limit
        self.mailbox_queue_bytes_limit = mailbox_bytes_limit
        self.console_queue_limit_bytes = console_limit
        self.mqtt_queue_limit = mqtt_limit

    @staticmethod
    def _require_positive(name: str, value: int) -> int:
        if value <= 0:
            raise ValueError(f"{name} must be a positive integer")
        return value

    def _normalize_topic_prefix(self) -> None:
        normalized = self._build_topic_prefix(self.mqtt_topic)
        self.mqtt_topic = normalized

    def _validate_flash_protection(self) -> None:
        """
        [SIL-2] Enforce Flash Wear Protection.
        Critical paths (filesystem root, mqtt spool) MUST be in RAM (/tmp).
        """
        root = self._normalize_path(
            self.file_system_root,
            field_name="file_system_root",
            require_absolute=True,
        )
        spool = self._normalize_path(
            self.mqtt_spool_dir,
            field_name="mqtt_spool_dir",
            require_absolute=True,
        )

        # 1. File System Component Root
        if not self.allow_non_tmp_paths:
            if not root.startswith("/tmp"):
                raise ValueError(
                    f"FLASH PROTECTION: file_system_root '{root}' is not in /tmp. "
                    "This prevents flash wear on the OpenWrt device. "
                    "Set 'allow_non_tmp_paths' to '1' in UCI if you REALLY need persistent storage."
                )

        # 2. MQTT Spool (ALWAYS in RAM)
        if not spool.startswith("/tmp"):
            raise ValueError(
                f"FLASH PROTECTION: mqtt_spool_dir '{spool}' is not in /tmp. "
                "MQTT spool writes frequently and must reside in RAM to prevent flash destruction."
            )

        self.file_system_root = root
        self.mqtt_spool_dir = spool

    def _validate_operational_limits(self) -> None:
        positive_int_fields = (
            "reconnect_delay",
            "status_interval",
            "process_timeout",
            "process_max_output_bytes",
            "process_max_concurrent",
            "serial_handshake_fatal_failures",
            "file_write_max_bytes",
            "file_storage_quota_bytes",
        )
        for field_name in positive_int_fields:
            value = getattr(self, field_name)
            validated = self._require_positive(field_name, int(value))
            setattr(self, field_name, validated)

        if self.file_storage_quota_bytes < self.file_write_max_bytes:
            raise ValueError("file_storage_quota_bytes must be greater than or equal to " "file_write_max_bytes")

        if self.watchdog_enabled:
            interval = self._require_positive_float(
                "watchdog_interval",
                float(self.watchdog_interval),
            )
            self.watchdog_interval = max(0.5, interval)

        self.bridge_summary_interval = max(
            0.0,
            float(self.bridge_summary_interval),
        )
        self.bridge_handshake_interval = max(
            0.0,
            float(self.bridge_handshake_interval),
        )

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

    @staticmethod
    def _require_positive_float(name: str, value: float) -> float:
        if value <= 0.0:
            raise ValueError(f"{name} must be a positive number")
        return value


def _load_raw_config() -> dict[str, Any]:
    """Load configuration from UCI with robust error handling (SIL 2)."""
    try:
        uci_values = get_uci_config()
        if uci_values:
            return uci_values
    except (OSError, ValueError) as err:
        # [SIL-2] Catch specific errors to differentiate operational issues from bugs.
        # Fallback to defaults is acceptable here to ensure Fail-Operational behavior.
        logger.error("Failed to load UCI configuration (Operational Error): %s", err)

    return get_default_config()


def load_runtime_config() -> RuntimeConfig:
    """Load configuration from UCI/defaults using msgspec for efficient validation."""

    raw_config = _load_raw_config()

    # Pre-process 'allowed_commands' since msgspec handles standard types
    if "allowed_commands" in raw_config:
        allowed_raw = raw_config["allowed_commands"]
        if isinstance(allowed_raw, str):
            commands = normalise_allowed_commands(allowed_raw.split())
            raw_config["allowed_commands"] = commands

    # Map 'debug' (from UCI) to 'debug_logging' (internal)
    if "debug" in raw_config and "debug_logging" not in raw_config:
        raw_config["debug_logging"] = parse_bool(raw_config.pop("debug"))

    # Pre-process 'serial_shared_secret' to handle string -> bytes conversion
    if "serial_shared_secret" in raw_config:
        secret = raw_config["serial_shared_secret"]
        if isinstance(secret, str):
            raw_config["serial_shared_secret"] = secret.strip().encode("utf-8")

    try:
        # msgspec handles type coercion (str -> int, str -> bool) via 'str_strict=False' (implied or manual)
        # However, for 'decoding' from a dict, we use 'convert'.
        # strict=False allows "1" -> True, "123" -> 123
        config = msgspec.convert(raw_config, RuntimeConfig, strict=False)
        return config
    except (msgspec.ValidationError, TypeError, ValueError) as e:
        logger.critical("Configuration validation failed: %s", e)
        # For resilience, we return the default config object.
        logger.warning("Falling back to safe defaults due to validation error.")
        config = msgspec.convert(get_default_config(), RuntimeConfig, strict=False)
        return config
