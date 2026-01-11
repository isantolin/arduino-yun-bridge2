"""Settings loader for the MCU Bridge daemon.

Configuration is loaded from OpenWrt UCI (package `mcubridge`, section
`general`) with sane defaults for non-OpenWrt environments.

Runtime configuration is intentionally **UCI-only**: environment variables are
not used as overrides.
"""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass, field

from ..common import (
    get_default_config,
    get_uci_config,
    normalise_allowed_commands,
    parse_bool,
    parse_float,
    parse_int,
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
    DEFAULT_STATUS_INTERVAL,
    DEFAULT_WATCHDOG_ENABLED,
    DEFAULT_WATCHDOG_INTERVAL,
    MIN_SERIAL_SHARED_SECRET_LEN,
)
from ..policy import AllowedCommandPolicy, TopicAuthorization
from ..rpc import protocol
from ..rpc.protocol import DEFAULT_BAUDRATE, DEFAULT_RETRY_LIMIT, DEFAULT_SAFE_BAUDRATE


logger = logging.getLogger(__name__)


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
        self.allowed_policy = AllowedCommandPolicy.from_iterable(self.allowed_commands)
        self.serial_response_timeout = max(
            self.serial_response_timeout, self.serial_retry_timeout * 2
        )
        self.serial_handshake_min_interval = max(
            0.0, self.serial_handshake_min_interval
        )
        self.serial_handshake_fatal_failures = self._require_positive(
            "serial_handshake_fatal_failures",
            int(self.serial_handshake_fatal_failures),
        )
        if not self.mqtt_tls:
            logger.warning(
                "MQTT TLS is disabled; MQTT credentials and payloads "
                "will be sent in plaintext."
            )
        else:
            if self.mqtt_tls_insecure:
                logger.warning(
                    "MQTT TLS hostname verification is disabled (mqtt_tls_insecure=1); "
                    "this is less secure and should be used only for known/self-hosted brokers."
                )
            if not self.mqtt_cafile:
                logger.info(
                    "MQTT TLS is enabled with no mqtt_cafile configured; using system trust store."
                )
        if not self.serial_shared_secret:
            raise ValueError("serial_shared_secret must be configured")
        if len(self.serial_shared_secret) < MIN_SERIAL_SHARED_SECRET_LEN:
            raise ValueError(
                "serial_shared_secret must be at least %d bytes"
                % MIN_SERIAL_SHARED_SECRET_LEN
            )
        if self.serial_shared_secret == b"changeme123":
            raise ValueError("serial_shared_secret placeholder is insecure")
        self.pending_pin_request_limit = max(1, self.pending_pin_request_limit)
        unique_symbols = {byte for byte in self.serial_shared_secret}
        if len(unique_symbols) < 4:
            raise ValueError(
                "serial_shared_secret must contain at least " "four distinct bytes"
            )
        self._validate_queue_limits()
        self._normalize_topic_prefix()
        self._normalize_paths()
        self._validate_operational_limits()

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
            raise ValueError(
                "mailbox_queue_bytes_limit must be greater than or equal to "
                "mailbox_queue_limit"
            )
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

    def _normalize_paths(self) -> None:
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

        # allow_non_tmp_paths is ONLY for the File component (file_system_root).
        if not self.allow_non_tmp_paths:
            if root != "/tmp" and not root.startswith("/tmp/"):
                logger.warning(
                    "FLASH PROTECTION: Overriding file_system_root outside /tmp (%s) to %s; "
                    "set allow_non_tmp_paths=1 to allow non-/tmp paths for file storage.",
                    root,
                    DEFAULT_FILE_SYSTEM_ROOT,
                )
                root = self._normalize_path(
                    DEFAULT_FILE_SYSTEM_ROOT,
                    field_name="file_system_root",
                    require_absolute=True,
                )

        # mqtt_spool_dir is always kept under /tmp to avoid flash wear.
        if spool != "/tmp" and not spool.startswith("/tmp/"):
            logger.warning(
                "FLASH PROTECTION: Overriding mqtt_spool_dir outside /tmp (%s) to %s; "
                "MQTT spool must live under /tmp.",
                spool,
                DEFAULT_MQTT_SPOOL_DIR,
            )
            spool = self._normalize_path(
                DEFAULT_MQTT_SPOOL_DIR,
                field_name="mqtt_spool_dir",
                require_absolute=True,
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
            raise ValueError(
                "file_storage_quota_bytes must be greater than or equal to "
                "file_write_max_bytes"
            )

        if self.watchdog_enabled:
            interval = self._require_positive_float(
                "watchdog_interval",
                float(self.watchdog_interval),
            )
            self.watchdog_interval = interval

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
        segments = [segment for segment in prefix.split("/") if segment]
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
        expanded = os.path.expanduser(candidate)
        normalized = os.path.abspath(expanded)
        if require_absolute and not os.path.isabs(expanded):
            raise ValueError(f"{field_name} must be an absolute path")
        return normalized

    @staticmethod
    def _require_positive_float(name: str, value: float) -> float:
        if value <= 0.0:
            raise ValueError(f"{name} must be a positive number")
        return value


def _load_raw_config() -> dict[str, str]:
    try:
        uci_values = get_uci_config()
        if uci_values:
            return uci_values
    except Exception:
        # get_uci_config already logs, simply fall back to defaults
        pass
    return get_default_config()


def _optional_path(path: str | None) -> str | None:
    if not path:
        return None
    candidate = path.strip()
    return candidate or None


def _raw_get_int(raw: dict[str, str], key: str, default: int) -> int:
    return parse_int(raw.get(key), default)


def _raw_get_bool(raw: dict[str, str], key: str, default: bool) -> bool:
    value = raw.get(key)
    return parse_bool(value) if value is not None else default


def load_runtime_config() -> RuntimeConfig:
    """Load configuration from UCI/defaults."""

    raw = _load_raw_config()

    debug_logging = parse_bool(raw.get("debug"))

    allowed_commands_raw = raw.get("allowed_commands", "")
    allowed_commands = normalise_allowed_commands(allowed_commands_raw.split())

    watchdog_enabled = _raw_get_bool(raw, "watchdog_enabled", False)
    watchdog_interval = max(
        0.5,
        parse_float(raw.get("watchdog_interval"), DEFAULT_WATCHDOG_INTERVAL),
    )

    mqtt_tls_value = raw.get("mqtt_tls")
    mqtt_tls = parse_bool(mqtt_tls_value) if mqtt_tls_value is not None else True

    mqtt_tls_insecure = _raw_get_bool(raw, "mqtt_tls_insecure", False)

    serial_secret_str = (raw.get("serial_shared_secret") or "").strip()
    serial_secret_bytes = (
        serial_secret_str.encode("utf-8") if serial_secret_str else b""
    )

    spool_dir = raw.get("mqtt_spool_dir", DEFAULT_MQTT_SPOOL_DIR)

    mqtt_cafile = _optional_path(raw.get("mqtt_cafile"))
    if mqtt_cafile is None and mqtt_tls:
        mqtt_cafile = DEFAULT_MQTT_CAFILE

    mqtt_certfile = _optional_path(raw.get("mqtt_certfile"))
    mqtt_keyfile = _optional_path(raw.get("mqtt_keyfile"))
    mqtt_user = _optional_path(raw.get("mqtt_user"))
    mqtt_pass = _optional_path(raw.get("mqtt_pass"))

    topic_authorization = TopicAuthorization(
        file_read=_raw_get_bool(raw, "mqtt_allow_file_read", True),
        file_write=_raw_get_bool(raw, "mqtt_allow_file_write", True),
        file_remove=_raw_get_bool(raw, "mqtt_allow_file_remove", True),
        datastore_get=_raw_get_bool(raw, "mqtt_allow_datastore_get", True),
        datastore_put=_raw_get_bool(raw, "mqtt_allow_datastore_put", True),
        mailbox_read=_raw_get_bool(raw, "mqtt_allow_mailbox_read", True),
        mailbox_write=_raw_get_bool(raw, "mqtt_allow_mailbox_write", True),
        shell_run=_raw_get_bool(raw, "mqtt_allow_shell_run", True),
        shell_run_async=_raw_get_bool(raw, "mqtt_allow_shell_run_async", True),
        shell_poll=_raw_get_bool(raw, "mqtt_allow_shell_poll", True),
        shell_kill=_raw_get_bool(raw, "mqtt_allow_shell_kill", True),
        console_input=_raw_get_bool(raw, "mqtt_allow_console_input", True),
        digital_write=_raw_get_bool(raw, "mqtt_allow_digital_write", True),
        digital_read=_raw_get_bool(raw, "mqtt_allow_digital_read", True),
        digital_mode=_raw_get_bool(raw, "mqtt_allow_digital_mode", True),
        analog_write=_raw_get_bool(raw, "mqtt_allow_analog_write", True),
        analog_read=_raw_get_bool(raw, "mqtt_allow_analog_read", True),
    )
    metrics_enabled = _raw_get_bool(raw, "metrics_enabled", False)

    metrics_host = (raw.get("metrics_host") or DEFAULT_METRICS_HOST).strip()
    metrics_port = parse_int(raw.get("metrics_port"), DEFAULT_METRICS_PORT)

    summary_interval = parse_float(
        raw.get("bridge_summary_interval"),
        float(DEFAULT_BRIDGE_SUMMARY_INTERVAL),
    )

    handshake_interval = parse_float(
        raw.get("bridge_handshake_interval"),
        float(DEFAULT_BRIDGE_HANDSHAKE_INTERVAL),
    )

    return RuntimeConfig(
        serial_port=raw.get("serial_port", DEFAULT_SERIAL_PORT),
        serial_baud=_raw_get_int(raw, "serial_baud", DEFAULT_BAUDRATE),
        serial_safe_baud=_raw_get_int(raw, "serial_safe_baud", DEFAULT_SAFE_BAUDRATE),
        mqtt_host=raw.get("mqtt_host", DEFAULT_MQTT_HOST),
        mqtt_port=_raw_get_int(raw, "mqtt_port", DEFAULT_MQTT_PORT),
        mqtt_user=_optional_path(mqtt_user),
        mqtt_pass=_optional_path(mqtt_pass),
        mqtt_tls=mqtt_tls,
        mqtt_tls_insecure=mqtt_tls_insecure,
        mqtt_cafile=mqtt_cafile,
        mqtt_certfile=_optional_path(mqtt_certfile),
        mqtt_keyfile=_optional_path(mqtt_keyfile),
        mqtt_topic=raw.get("mqtt_topic", protocol.MQTT_DEFAULT_TOPIC_PREFIX),
        allowed_commands=allowed_commands,
        file_system_root=raw.get("file_system_root", DEFAULT_FILE_SYSTEM_ROOT),
        process_timeout=_raw_get_int(raw, "process_timeout", DEFAULT_PROCESS_TIMEOUT),
        file_write_max_bytes=max(
            1,
            _raw_get_int(
                raw,
                "file_write_max_bytes",
                DEFAULT_FILE_WRITE_MAX_BYTES,
            ),
        ),
        file_storage_quota_bytes=max(
            1,
            _raw_get_int(
                raw,
                "file_storage_quota_bytes",
                DEFAULT_FILE_STORAGE_QUOTA_BYTES,
            ),
        ),
        mqtt_queue_limit=max(
            1,
            _raw_get_int(raw, "mqtt_queue_limit", DEFAULT_MQTT_QUEUE_LIMIT),
        ),
        reconnect_delay=_raw_get_int(raw, "reconnect_delay", DEFAULT_RECONNECT_DELAY),
        status_interval=_raw_get_int(raw, "status_interval", DEFAULT_STATUS_INTERVAL),
        debug_logging=debug_logging,
        console_queue_limit_bytes=_raw_get_int(
            raw,
            "console_queue_limit_bytes", DEFAULT_CONSOLE_QUEUE_LIMIT_BYTES
        ),
        mailbox_queue_limit=_raw_get_int(
            raw,
            "mailbox_queue_limit", DEFAULT_MAILBOX_QUEUE_LIMIT
        ),
        mailbox_queue_bytes_limit=_raw_get_int(
            raw,
            "mailbox_queue_bytes_limit", DEFAULT_MAILBOX_QUEUE_BYTES_LIMIT
        ),
        pending_pin_request_limit=_raw_get_int(
            raw,
            "pending_pin_request_limit",
            DEFAULT_PENDING_PIN_REQUESTS,
        ),
        serial_retry_timeout=parse_float(
            raw.get("serial_retry_timeout"), DEFAULT_SERIAL_RETRY_TIMEOUT
        ),
        serial_response_timeout=parse_float(
            raw.get("serial_response_timeout"), DEFAULT_SERIAL_RESPONSE_TIMEOUT
        ),
        serial_retry_attempts=max(
            1, _raw_get_int(raw, "serial_retry_attempts", DEFAULT_RETRY_LIMIT)
        ),
        serial_handshake_min_interval=max(
            0.0,
            parse_float(
                raw.get("serial_handshake_min_interval"),
                DEFAULT_SERIAL_HANDSHAKE_MIN_INTERVAL,
            ),
        ),
        serial_handshake_fatal_failures=max(
            1,
            _raw_get_int(
                raw,
                "serial_handshake_fatal_failures",
                DEFAULT_SERIAL_HANDSHAKE_FATAL_FAILURES,
            ),
        ),
        watchdog_enabled=watchdog_enabled,
        watchdog_interval=watchdog_interval,
        topic_authorization=topic_authorization,
        serial_shared_secret=serial_secret_bytes,
        mqtt_spool_dir=spool_dir,
        process_max_output_bytes=max(
            1024,
            _raw_get_int(
                raw,
                "process_max_output_bytes",
                DEFAULT_PROCESS_MAX_OUTPUT_BYTES,
            ),
        ),
        process_max_concurrent=max(
            1,
            _raw_get_int(
                raw,
                "process_max_concurrent",
                DEFAULT_PROCESS_MAX_CONCURRENT,
            ),
        ),
        metrics_enabled=metrics_enabled,
        metrics_host=metrics_host,
        metrics_port=max(0, metrics_port),
        bridge_summary_interval=summary_interval,
        bridge_handshake_interval=handshake_interval,
        allow_non_tmp_paths=_raw_get_bool(raw, "allow_non_tmp_paths", False),
    )
