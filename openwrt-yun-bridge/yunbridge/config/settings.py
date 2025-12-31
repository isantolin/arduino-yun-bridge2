"""Runtime configuration management with Flash protection."""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass

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

    MODERNIZACIÓN PYTHON 3.13:
    Esta clase usa 'dataclasses' para evitar escribir getters/setters repetitivos.
    Cada campo aquí definido se valida automáticamente en el método 'load'.
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
    # Default is True, but can be disabled via UCI
    # 'yunbridge.general.mqtt_allow_xxx = 0'
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

    # Use ClassVar for constants if needed, but here we don't have any yet
    # that aren't imported.

    @classmethod
    def load(cls) -> RuntimeConfig:
        """
        Load configuration prioritizing ENV > UCI > Defaults.

        This method acts as the central factory, resolving conflicts between
        Environment Variables (Docker/Dev) and UCI (OpenWrt System).
        """
        # 1. Load UCI (or defaults if missing)
        uci = get_uci_config()

        # 2. Helpers for resolution (Reduces code duplication)
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

        # 3. Build instance with resolved values
        config = cls(
            # Serial
            serial_port=resolve_str("serial_port") or DEFAULT_SERIAL_PORT,
            serial_baud=resolve_int("serial_baud", 115200),
            serial_safe_baud=resolve_int("serial_safe_baud", 115200),
            serial_shared_secret=None,  # Handled specifically below
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
            # MQTT
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
            # [CRITICAL] This path is validated later to ensure it's in RAM
            mqtt_spool_dir=resolve_str("mqtt_spool_dir", ENV_MQTT_SPOOL_DIR)
            or DEFAULT_MQTT_SPOOL_DIR,
            mqtt_queue_limit=resolve_int(
                "mqtt_queue_limit", DEFAULT_MQTT_QUEUE_LIMIT
            ),
            # Filesystem
            file_system_root=resolve_str("file_system_root")
            or DEFAULT_FILE_SYSTEM_ROOT,
            file_write_max_bytes=resolve_int(
                "file_write_max_bytes", DEFAULT_FILE_WRITE_MAX_BYTES
            ),
            file_storage_quota_bytes=resolve_int(
                "file_storage_quota_bytes", DEFAULT_FILE_STORAGE_QUOTA_BYTES
            ),
            # Components
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
            # Operational
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
            # Metrics
            metrics_enabled=resolve_bool(
                "metrics_enabled", False, ENV_METRICS_ENABLED
            ),
            metrics_host=resolve_str("metrics_host", ENV_METRICS_HOST)
            or DEFAULT_METRICS_HOST,
            metrics_port=resolve_int(
                "metrics_port", DEFAULT_METRICS_PORT, ENV_METRICS_PORT
            ),
            # Watchdog
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

        # Dynamic Permission loading from UCI
        # Checks for keys like 'mqtt_allow_file_read' automatically
        for field_name in cls.__dataclass_fields__:
            if field_name.startswith("mqtt_allow_"):
                default_val = getattr(config, field_name)
                val = resolve_bool(field_name, default_val)
                setattr(config, field_name, val)

        # Secure Secret Handling
        secret_str = resolve_str("serial_shared_secret", ENV_SERIAL_SECRET)
        if secret_str:
            config.serial_shared_secret = secret_str.encode("utf-8")
        else:
            config.serial_shared_secret = DEFAULT_SERIAL_SHARED_SECRET

        # [HARDENING] Apply strict validation logic
        config._validate_operational_limits()
        return config

    def _validate_operational_limits(self) -> None:
        """
        Enforce safety bounds on configuration values to protect the hardware.

        This method is critical for:
        1. Flash Protection: Ensuring high-write directories are in RAM.
        2. DoS Protection: Limiting queue sizes and concurrency.
        """
        # [HARDENING] Flash Memory Protection Logic
        # OpenWrt uses /tmp (tmpfs) for volatile storage. Writing high-frequency
        # MQTT spool data to persistent flash (/root, /etc) will destroy the
        # specific block in weeks/months. We enforce RAM usage here.
        safe_prefixes = ("/tmp/", "/var/run/", "/dev/null")
        is_safe_path = any(
            self.mqtt_spool_dir.startswith(p) for p in safe_prefixes
        )

        if not is_safe_path:
            # We override unsafe configuration to protect the hardware.
            # Logging might not be up yet, but this is a critical safety fallback.
            # The daemon will eventually log the path being used.
            self.mqtt_spool_dir = DEFAULT_MQTT_SPOOL_DIR

        # Sanity Checks for Timings
        self.serial_retry_timeout = max(0.1, self.serial_retry_timeout)
        self.reconnect_delay = max(1.0, self.reconnect_delay)
        self.status_interval = max(1, self.status_interval)

        # Security: Enforce minimum secret length
        if (
            self.serial_shared_secret
            and len(self.serial_shared_secret) < MIN_SERIAL_SHARED_SECRET_LEN
        ):
            # Fallback to no secret if it's too weak
            self.serial_shared_secret = None

        # Resource Protection: Clamp queue limits to prevent OOM on small
        # routers (16MB/32MB RAM)
        self.mqtt_queue_limit = min(self.mqtt_queue_limit, 2048)
        self.process_max_concurrent = min(self.process_max_concurrent, 16)
