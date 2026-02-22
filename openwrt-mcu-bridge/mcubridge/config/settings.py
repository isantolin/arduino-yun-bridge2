"""Settings loader for the MCU Bridge daemon.

Configuration is loaded from OpenWrt UCI (package `mcubridge`, section
`general`) with sane defaults for non-OpenWrt environments.

Runtime configuration is intentionally **UCI-only**: environment variables are
not used as overrides.
"""

from __future__ import annotations

import logging
import os
from typing import Annotated, Any

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
    serial_baud: Annotated[int, msgspec.Meta(ge=300)] = DEFAULT_BAUDRATE
    serial_safe_baud: Annotated[int, msgspec.Meta(ge=300)] = DEFAULT_SAFE_BAUDRATE
    mqtt_host: str = DEFAULT_MQTT_HOST
    mqtt_port: Annotated[int, msgspec.Meta(ge=1, le=65535)] = DEFAULT_MQTT_PORT
    mqtt_user: str | None = None
    mqtt_pass: str | None = None
    mqtt_tls: bool = True
    mqtt_cafile: str | None = DEFAULT_MQTT_CAFILE
    mqtt_certfile: str | None = None
    mqtt_keyfile: str | None = None
    mqtt_topic: str = MQTT_DEFAULT_TOPIC_PREFIX
    allowed_commands: tuple[str, ...] = ()
    file_system_root: str = DEFAULT_FILE_SYSTEM_ROOT
    process_timeout: Annotated[int, msgspec.Meta(ge=1)] = DEFAULT_PROCESS_TIMEOUT

    mqtt_tls_insecure: bool = DEFAULT_MQTT_TLS_INSECURE
    file_write_max_bytes: Annotated[int, msgspec.Meta(ge=1)] = DEFAULT_FILE_WRITE_MAX_BYTES
    file_storage_quota_bytes: Annotated[int, msgspec.Meta(ge=1)] = DEFAULT_FILE_STORAGE_QUOTA_BYTES

    allowed_policy: AllowedCommandPolicy | None = None

    mqtt_queue_limit: Annotated[int, msgspec.Meta(ge=0)] = DEFAULT_MQTT_QUEUE_LIMIT
    reconnect_delay: Annotated[int, msgspec.Meta(ge=1)] = DEFAULT_RECONNECT_DELAY
    status_interval: Annotated[int, msgspec.Meta(ge=1)] = DEFAULT_STATUS_INTERVAL
    debug_logging: bool = DEFAULT_DEBUG_LOGGING
    console_queue_limit_bytes: Annotated[int, msgspec.Meta(ge=1)] = DEFAULT_CONSOLE_QUEUE_LIMIT_BYTES
    mailbox_queue_limit: Annotated[int, msgspec.Meta(ge=1)] = DEFAULT_MAILBOX_QUEUE_LIMIT
    mailbox_queue_bytes_limit: Annotated[int, msgspec.Meta(ge=1)] = DEFAULT_MAILBOX_QUEUE_BYTES_LIMIT
    pending_pin_request_limit: Annotated[int, msgspec.Meta(ge=1)] = DEFAULT_PENDING_PIN_REQUESTS
    serial_retry_timeout: Annotated[float, msgspec.Meta(ge=0.01)] = DEFAULT_SERIAL_RETRY_TIMEOUT
    serial_response_timeout: Annotated[float, msgspec.Meta(ge=0.02)] = DEFAULT_SERIAL_RESPONSE_TIMEOUT
    serial_retry_attempts: Annotated[int, msgspec.Meta(ge=0)] = DEFAULT_RETRY_LIMIT
    serial_handshake_min_interval: Annotated[float, msgspec.Meta(ge=0.0)] = DEFAULT_SERIAL_HANDSHAKE_MIN_INTERVAL
    serial_handshake_fatal_failures: Annotated[int, msgspec.Meta(ge=1)] = DEFAULT_SERIAL_HANDSHAKE_FATAL_FAILURES
    watchdog_enabled: bool = True
    watchdog_interval: Annotated[float, msgspec.Meta(ge=0.1)] = DEFAULT_WATCHDOG_INTERVAL
    topic_authorization: TopicAuthorization = TopicAuthorization()

    # msgspec handle bytes naturally.
    # [SIL-2] SECURITY: This default enables initial setup only.
    # It MUST be rotated using 'mcubridge-rotate-credentials'.
    serial_shared_secret: Annotated[bytes, msgspec.Meta(min_length=MIN_SERIAL_SHARED_SECRET_LEN)] = DEFAULT_SERIAL_SHARED_SECRET

    mqtt_spool_dir: str = DEFAULT_MQTT_SPOOL_DIR
    process_max_output_bytes: Annotated[int, msgspec.Meta(ge=1)] = DEFAULT_PROCESS_MAX_OUTPUT_BYTES
    process_max_concurrent: Annotated[int, msgspec.Meta(ge=1)] = DEFAULT_PROCESS_MAX_CONCURRENT
    metrics_enabled: bool = DEFAULT_METRICS_ENABLED
    metrics_host: str = DEFAULT_METRICS_HOST
    metrics_port: Annotated[int, msgspec.Meta(ge=1, le=65535)] = DEFAULT_METRICS_PORT
    bridge_summary_interval: Annotated[float, msgspec.Meta(ge=0.0)] = DEFAULT_BRIDGE_SUMMARY_INTERVAL
    bridge_handshake_interval: Annotated[float, msgspec.Meta(ge=0.0)] = DEFAULT_BRIDGE_HANDSHAKE_INTERVAL
    allow_non_tmp_paths: bool = DEFAULT_ALLOW_NON_TMP_PATHS

    @property
    def tls_enabled(self) -> bool:
        return self.mqtt_tls

    def __post_init__(self) -> None:
        self.allowed_policy = AllowedCommandPolicy.from_iterable(self.allowed_commands)

        # [SIL-2] Strict Semantic Validations
        if self.serial_response_timeout < self.serial_retry_timeout * 2:
             raise ValueError("serial_response_timeout must be at least 2x serial_retry_timeout")

        if self.watchdog_enabled and self.watchdog_interval < 0.5:
             raise ValueError("watchdog_interval must be >= 0.5s when enabled")

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
        if self.serial_shared_secret == b"changeme123":
            raise ValueError("serial_shared_secret placeholder is insecure")

        unique_symbols = {byte for byte in self.serial_shared_secret}
        if len(unique_symbols) < 4:
            raise ValueError("serial_shared_secret must contain at least four distinct bytes")

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

    # [SIL-2] Boundary Normalization: Clean inputs before strict validation

    # 1. Normalize strings (strip and convert empty to None where applicable)
    for key in ("mqtt_user", "mqtt_pass", "mqtt_cafile", "mqtt_certfile", "mqtt_keyfile"):
        if key in raw_config:
            val = str(raw_config[key]).strip()
            raw_config[key] = val if val else None

    # 2. Pre-process 'allowed_commands'
    if "allowed_commands" in raw_config:
        allowed_raw = raw_config["allowed_commands"]
        if isinstance(allowed_raw, str):
            raw_config["allowed_commands"] = normalise_allowed_commands(allowed_raw.split())

    # 3. Map 'debug' (from UCI) to 'debug_logging'
    if "debug" in raw_config:
        raw_config["debug_logging"] = parse_bool(raw_config.pop("debug"))

    # 4. Normalize MQTT topic prefix
    if "mqtt_topic" in raw_config:
        prefix = str(raw_config["mqtt_topic"])
        segments = [s.strip() for s in prefix.split("/") if s.strip()]
        normalized = "/".join(segments)
        if not normalized:
            raise ValueError("mqtt_topic must contain at least one segment")
        raw_config["mqtt_topic"] = normalized

    # 5. Normalize Paths
    if "file_system_root" in raw_config:
        raw_config["file_system_root"] = os.path.abspath(os.path.expanduser(str(raw_config["file_system_root"])))
    if "mqtt_spool_dir" in raw_config:
        raw_config["mqtt_spool_dir"] = os.path.abspath(os.path.expanduser(str(raw_config["mqtt_spool_dir"])))

    # 6. Pre-process 'serial_shared_secret'
    if "serial_shared_secret" in raw_config:
        secret = raw_config["serial_shared_secret"]
        if isinstance(secret, str):
            raw_config["serial_shared_secret"] = secret.strip().encode("utf-8")

    try:
        config = msgspec.convert(raw_config, RuntimeConfig, strict=False)
        return config
    except (msgspec.ValidationError, TypeError, ValueError) as e:
        if source == "test":
            raise ValueError(f"Configuration validation failed: {e}") from e
        logger.critical("Configuration validation failed: %s", e)
        logger.warning("Falling back to safe defaults due to validation error.")
        return msgspec.convert(get_default_config(), RuntimeConfig, strict=False)
