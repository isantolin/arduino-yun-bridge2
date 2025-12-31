"""Runtime configuration management with Flash protection."""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass
from typing import Any

from ..common import (
    get_uci_config,
    normalise_allowed_commands,
    parse_bool,
    parse_float,
    parse_int,
)
from ..const import (
    DEFAULT_BRIDGE_HANDSHAKE_INTERVAL,
    DEFAULT_BRIDGE_SUMMARY_INTERVAL,
    DEFAULT_CONSOLE_QUEUE_LIMIT_BYTES,
    DEFAULT_FILE_STORAGE_QUOTA_BYTES,
    DEFAULT_FILE_SYSTEM_ROOT,
    DEFAULT_FILE_WRITE_MAX_BYTES,
    DEFAULT_MAILBOX_QUEUE_BYTES_LIMIT,
    DEFAULT_MAILBOX_QUEUE_LIMIT,
    DEFAULT_METRICS_HOST,
    DEFAULT_METRICS_PORT,
    DEFAULT_MQTT_CAFILE,
    DEFAULT_MQTT_HOST,
    DEFAULT_MQTT_PORT,
    DEFAULT_MQTT_QUEUE_LIMIT,
    DEFAULT_MQTT_SPOOL_DIR,
    DEFAULT_MQTT_TOPIC,
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
    ENV_BRIDGE_HANDSHAKE_INTERVAL,
    ENV_BRIDGE_SUMMARY_INTERVAL,
    ENV_DEBUG,
    ENV_DISABLE_WATCHDOG,
    ENV_METRICS_ENABLED,
    ENV_METRICS_HOST,
    ENV_METRICS_PORT,
    ENV_MQTT_CAFILE,
    ENV_MQTT_CERTFILE,
    ENV_MQTT_KEYFILE,
    ENV_MQTT_PASS,
    ENV_MQTT_SPOOL_DIR,
    ENV_MQTT_USER,
    ENV_SERIAL_SECRET,
    ENV_WATCHDOG_INTERVAL,
    MIN_SERIAL_SHARED_SECRET_LEN,
    SUPERVISOR_DEFAULT_MAX_BACKOFF,
    SUPERVISOR_DEFAULT_MIN_BACKOFF,
    SUPERVISOR_DEFAULT_RESTART_INTERVAL,
)

logger = logging.getLogger(__name__)


@dataclass
class RuntimeConfig:
    """
    Consolidated configuration from Defaults, UCI, and Environment.

    This class uses 'dataclasses' for boilerplate reduction but includes
    compatibility properties to match the legacy API expected by consumers.
    """

    # --- Serial / UART Configuration ---
    serial_port: str
    serial_baud: int
    serial_safe_baud: int
    serial_shared_secret: bytes | None
    serial_retry_timeout: float
    serial_response_timeout: float
    serial_retry_attempts: int
    serial_handshake_min_interval: float
    serial_handshake_fatal_failures: int

    # --- MQTT Broker Configuration ---
    mqtt_host: str
    mqtt_port: int
    mqtt_tls: bool
    mqtt_cafile: str
    mqtt_certfile: str | None
    mqtt_keyfile: str | None
    mqtt_user: str | None
    mqtt_pass: str | None
    mqtt_topic: str
    mqtt_spool_dir: str
    mqtt_queue_limit: int

    # --- Filesystem & Components Limits ---
    file_system_root: str
    file_write_max_bytes: int
    file_storage_quota_bytes: int
    process_timeout: int
    process_max_output_bytes: int
    process_max_concurrent: int
    console_queue_limit_bytes: int
    mailbox_queue_limit: int
    mailbox_queue_bytes_limit: int
    pending_pin_request_limit: int

    # --- Operational Parameters ---
    reconnect_delay: float
    status_interval: int
    bridge_summary_interval: int
    bridge_handshake_interval: int
    debug_logging: bool
    allowed_commands: tuple[str, ...]

    # --- Telemetry / Metrics ---
    metrics_enabled: bool
    metrics_host: str
    metrics_port: int

    # --- Watchdog & Supervision ---
    watchdog_enabled: bool
    watchdog_interval: float
    supervisor_restart_interval: float
    supervisor_min_backoff: float
    supervisor_max_backoff: float

    # --- Security Policies (MQTT Actions) ---
    mqtt_allow_file_read: bool = True
    mqtt_allow_file_write: bool = True
    mqtt_allow_file_remove: bool = True
    mqtt_allow_datastore_get: bool = True
    mqtt_allow_datastore_put: bool = True
    mqtt_allow_mailbox_read: bool = True
    mqtt_allow_mailbox_write: bool = True
    mqtt_allow_shell_run: bool = True
    mqtt_allow_shell_run_async: bool = True
    mqtt_allow_shell_poll: bool = True
    mqtt_allow_shell_kill: bool = True
    mqtt_allow_console_input: bool = True
    mqtt_allow_digital_write: bool = True
    mqtt_allow_digital_read: bool = True
    mqtt_allow_digital_mode: bool = True
    mqtt_allow_analog_write: bool = True
    mqtt_allow_analog_read: bool = True

    @classmethod
    def load(cls) -> RuntimeConfig:
        """Load configuration prioritizing ENV > UCI > Defaults."""
        uci = get_uci_config()

        def resolve_str(key: str, env_var: str | None = None) -> str:
            val = os.environ.get(env_var) if env_var else None
            if val is None:
                val = uci.get(key, "")
            return val

        def resolve_int(
            key: str, default: int, env_var: str | None = None
        ) -> int:
            val = os.environ.get(env_var) if env_var else None
            if val is None:
                val = uci.get(key)
            return parse_int(val, default)

        def resolve_float(
            key: str, default: float, env_var: str | None = None
        ) -> float:
            val = os.environ.get(env_var) if env_var else None
            if val is None:
                val = uci.get(key)
            return parse_float(val, default)

        def resolve_bool(
            key: str, default: bool = False, env_var: str | None = None
        ) -> bool:
            val = os.environ.get(env_var) if env_var else None
            if val is None:
                val = uci.get(key)
                if val is None:
                    return default
            return parse_bool(val)

        config = cls(
            serial_port=resolve_str("serial_port") or DEFAULT_SERIAL_PORT,
            serial_baud=resolve_int("serial_baud", 115200),
            serial_safe_baud=resolve_int("serial_safe_baud", 115200),
            serial_shared_secret=None,
            serial_retry_timeout=resolve_float(
                "serial_retry_timeout", DEFAULT_SERIAL_RETRY_TIMEOUT
            ),
            serial_response_timeout=resolve_float(
                "serial_response_timeout", DEFAULT_SERIAL_RESPONSE_TIMEOUT
            ),
            serial_retry_attempts=resolve_int("serial_retry_attempts", 5),
            serial_handshake_min_interval=resolve_float(
                "serial_handshake_min_interval",
                DEFAULT_SERIAL_HANDSHAKE_MIN_INTERVAL,
            ),
            serial_handshake_fatal_failures=resolve_int(
                "serial_handshake_fatal_failures",
                DEFAULT_SERIAL_HANDSHAKE_FATAL_FAILURES,
            ),
            mqtt_host=resolve_str("mqtt_host") or DEFAULT_MQTT_HOST,
            mqtt_port=resolve_int("mqtt_port", DEFAULT_MQTT_PORT),
            mqtt_tls=resolve_bool("mqtt_tls", True),
            mqtt_cafile=resolve_str("mqtt_cafile", ENV_MQTT_CAFILE)
            or DEFAULT_MQTT_CAFILE,
            mqtt_certfile=resolve_str("mqtt_certfile", ENV_MQTT_CERTFILE)
            or None,
            mqtt_keyfile=resolve_str("mqtt_keyfile", ENV_MQTT_KEYFILE) or None,
            mqtt_user=resolve_str("mqtt_user", ENV_MQTT_USER) or None,
            mqtt_pass=resolve_str("mqtt_pass", ENV_MQTT_PASS) or None,
            mqtt_topic=resolve_str("mqtt_topic") or DEFAULT_MQTT_TOPIC,
            mqtt_spool_dir=resolve_str("mqtt_spool_dir", ENV_MQTT_SPOOL_DIR)
            or DEFAULT_MQTT_SPOOL_DIR,
            mqtt_queue_limit=resolve_int(
                "mqtt_queue_limit", DEFAULT_MQTT_QUEUE_LIMIT
            ),
            file_system_root=resolve_str("file_system_root")
            or DEFAULT_FILE_SYSTEM_ROOT,
            file_write_max_bytes=resolve_int(
                "file_write_max_bytes", DEFAULT_FILE_WRITE_MAX_BYTES
            ),
            file_storage_quota_bytes=resolve_int(
                "file_storage_quota_bytes", DEFAULT_FILE_STORAGE_QUOTA_BYTES
            ),
            process_timeout=resolve_int(
                "process_timeout", DEFAULT_PROCESS_TIMEOUT
            ),
            process_max_output_bytes=resolve_int(
                "process_max_output_bytes", DEFAULT_PROCESS_MAX_OUTPUT_BYTES
            ),
            process_max_concurrent=resolve_int(
                "process_max_concurrent", DEFAULT_PROCESS_MAX_CONCURRENT
            ),
            console_queue_limit_bytes=resolve_int(
                "console_queue_limit_bytes", DEFAULT_CONSOLE_QUEUE_LIMIT_BYTES
            ),
            mailbox_queue_limit=resolve_int(
                "mailbox_queue_limit", DEFAULT_MAILBOX_QUEUE_LIMIT
            ),
            mailbox_queue_bytes_limit=resolve_int(
                "mailbox_queue_bytes_limit", DEFAULT_MAILBOX_QUEUE_BYTES_LIMIT
            ),
            pending_pin_request_limit=resolve_int(
                "pending_pin_request_limit", DEFAULT_PENDING_PIN_REQUESTS
            ),
            reconnect_delay=resolve_float(
                "reconnect_delay", DEFAULT_RECONNECT_DELAY
            ),
            status_interval=resolve_int(
                "status_interval", DEFAULT_STATUS_INTERVAL
            ),
            bridge_summary_interval=resolve_int(
                "bridge_summary_interval",
                DEFAULT_BRIDGE_SUMMARY_INTERVAL,
                ENV_BRIDGE_SUMMARY_INTERVAL,
            ),
            bridge_handshake_interval=resolve_int(
                "bridge_handshake_interval",
                DEFAULT_BRIDGE_HANDSHAKE_INTERVAL,
                ENV_BRIDGE_HANDSHAKE_INTERVAL,
            ),
            debug_logging=resolve_bool("debug", False, ENV_DEBUG),
            allowed_commands=normalise_allowed_commands(
                resolve_str("allowed_commands").split()
            ),
            metrics_enabled=resolve_bool(
                "metrics_enabled", False, ENV_METRICS_ENABLED
            ),
            metrics_host=resolve_str("metrics_host", ENV_METRICS_HOST)
            or DEFAULT_METRICS_HOST,
            metrics_port=resolve_int(
                "metrics_port", DEFAULT_METRICS_PORT, ENV_METRICS_PORT
            ),
            watchdog_enabled=not resolve_bool(
                "disable_watchdog", False, ENV_DISABLE_WATCHDOG
            ),
            watchdog_interval=resolve_float(
                "watchdog_interval",
                DEFAULT_WATCHDOG_INTERVAL,
                ENV_WATCHDOG_INTERVAL,
            ),
            supervisor_restart_interval=SUPERVISOR_DEFAULT_RESTART_INTERVAL,
            supervisor_min_backoff=SUPERVISOR_DEFAULT_MIN_BACKOFF,
            supervisor_max_backoff=SUPERVISOR_DEFAULT_MAX_BACKOFF,
        )

        for field_name in cls.__dataclass_fields__:
            if field_name.startswith("mqtt_allow_"):
                default_val = getattr(config, field_name)
                val = resolve_bool(field_name, default_val)
                setattr(config, field_name, val)

        secret_str = resolve_str("serial_shared_secret", ENV_SERIAL_SECRET)
        if secret_str:
            config.serial_shared_secret = secret_str.encode("utf-8")
        else:
            config.serial_shared_secret = DEFAULT_SERIAL_SHARED_SECRET

        config._validate_operational_limits()
        return config

    def _validate_operational_limits(self) -> None:
        """Enforce safety bounds on configuration values."""
        safe_prefixes = ("/tmp/", "/var/run/", "/dev/null")
        is_safe_path = any(
            self.mqtt_spool_dir.startswith(p) for p in safe_prefixes
        )

        if not is_safe_path:
            self.mqtt_spool_dir = DEFAULT_MQTT_SPOOL_DIR

        self.serial_retry_timeout = max(0.1, self.serial_retry_timeout)
        self.reconnect_delay = max(1.0, self.reconnect_delay)
        self.status_interval = max(1, self.status_interval)

        if (
            self.serial_shared_secret
            and len(self.serial_shared_secret) < MIN_SERIAL_SHARED_SECRET_LEN
        ):
            self.serial_shared_secret = None

        self.mqtt_queue_limit = min(self.mqtt_queue_limit, 2048)
        self.process_max_concurrent = min(self.process_max_concurrent, 16)

    # --- Compatibility Properties ---

    @property
    def tls_enabled(self) -> bool:
        """Alias for mqtt_tls used by mqtt transport."""
        return self.mqtt_tls

    @property
    def allowed_policy(self) -> dict[str, bool]:
        """
        Return a dictionary of all mqtt_allow_* flags.
        Used by context.py for policy enforcement.
        """
        return {
            k: getattr(self, k)
            for k in self.__dataclass_fields__
            if k.startswith("mqtt_allow_")
        }

    @property
    def topic_authorization(self) -> dict[str, Any]:
        """
        Return topic structure configuration.
        Used by context.py to validate topic access.
        """
        # Based on typical usage, this likely returns the configured root topic
        # or a structure defining read/write paths. Assuming simple structure
        # based on mqtt_topic for now.
        return {
            "root": self.mqtt_topic,
            # Add other topic-related config if needed by consumers
        }


def load_runtime_config() -> RuntimeConfig:
    """
    Legacy helper function to load configuration.
    Wraps RuntimeConfig.load() for backward compatibility with daemon.py.
    """
    return RuntimeConfig.load()
