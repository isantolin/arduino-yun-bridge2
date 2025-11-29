"""Settings loader for the Yun Bridge daemon.

This module centralises configuration loading from UCI and environment
variables so the rest of the code can depend on a strongly typed
RuntimeConfig instance.
"""
from __future__ import annotations

from dataclasses import dataclass, field
import logging
import os
from typing import Dict, Optional, Tuple

from ..common import (
    get_default_config,
    get_uci_config,
    normalise_allowed_commands,
)
from ..const import (
    DEFAULT_CREDENTIALS_FILE,
    DEFAULT_CONSOLE_QUEUE_LIMIT_BYTES,
    DEFAULT_FILE_SYSTEM_ROOT,
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
    DEFAULT_SERIAL_BAUD,
    DEFAULT_SERIAL_HANDSHAKE_MIN_INTERVAL,
    DEFAULT_SERIAL_PORT,
    DEFAULT_SERIAL_RESPONSE_TIMEOUT,
    DEFAULT_SERIAL_RETRY_ATTEMPTS,
    DEFAULT_SERIAL_RETRY_TIMEOUT,
    DEFAULT_SERIAL_SHARED_SECRET,
    DEFAULT_STATUS_INTERVAL,
    DEFAULT_WATCHDOG_INTERVAL,
    MIN_SERIAL_SHARED_SECRET_LEN,
)
from ..policy import AllowedCommandPolicy, TopicAuthorization
from .credentials import load_credentials_file, lookup_credential


logger = logging.getLogger(__name__)


@dataclass(slots=True)
class RuntimeConfig:
    """Strongly typed configuration for the daemon."""

    serial_port: str
    serial_baud: int
    mqtt_host: str
    mqtt_port: int
    mqtt_user: Optional[str]
    mqtt_pass: Optional[str]
    mqtt_tls: bool
    mqtt_cafile: Optional[str]
    mqtt_certfile: Optional[str]
    mqtt_keyfile: Optional[str]
    mqtt_topic: str
    allowed_commands: Tuple[str, ...]
    file_system_root: str
    process_timeout: int
    mqtt_queue_limit: int = DEFAULT_MQTT_QUEUE_LIMIT
    reconnect_delay: int = DEFAULT_RECONNECT_DELAY
    status_interval: int = DEFAULT_STATUS_INTERVAL
    debug_logging: bool = False
    console_queue_limit_bytes: int = DEFAULT_CONSOLE_QUEUE_LIMIT_BYTES
    mailbox_queue_limit: int = DEFAULT_MAILBOX_QUEUE_LIMIT
    mailbox_queue_bytes_limit: int = DEFAULT_MAILBOX_QUEUE_BYTES_LIMIT
    pending_pin_request_limit: int = DEFAULT_PENDING_PIN_REQUESTS
    serial_retry_timeout: float = DEFAULT_SERIAL_RETRY_TIMEOUT
    serial_response_timeout: float = DEFAULT_SERIAL_RESPONSE_TIMEOUT
    serial_retry_attempts: int = DEFAULT_SERIAL_RETRY_ATTEMPTS
    serial_handshake_min_interval: float = (
        DEFAULT_SERIAL_HANDSHAKE_MIN_INTERVAL
    )
    watchdog_enabled: bool = False
    watchdog_interval: float = DEFAULT_WATCHDOG_INTERVAL
    topic_authorization: TopicAuthorization = field(
        default_factory=TopicAuthorization
    )
    allowed_policy: AllowedCommandPolicy = field(init=False)
    serial_shared_secret: bytes = field(repr=False, default=b"")
    mqtt_spool_dir: str = DEFAULT_MQTT_SPOOL_DIR
    process_max_output_bytes: int = DEFAULT_PROCESS_MAX_OUTPUT_BYTES
    process_max_concurrent: int = DEFAULT_PROCESS_MAX_CONCURRENT
    credentials_file: str = DEFAULT_CREDENTIALS_FILE
    metrics_enabled: bool = False
    metrics_host: str = DEFAULT_METRICS_HOST
    metrics_port: int = DEFAULT_METRICS_PORT

    @property
    def tls_enabled(self) -> bool:
        return self.mqtt_tls and bool(self.mqtt_cafile)

    def __post_init__(self) -> None:
        object.__setattr__(
            self,
            "allowed_policy",
            AllowedCommandPolicy.from_iterable(self.allowed_commands),
        )
        object.__setattr__(
            self,
            "serial_response_timeout",
            max(self.serial_response_timeout, self.serial_retry_timeout * 2),
        )
        object.__setattr__(
            self,
            "serial_handshake_min_interval",
            max(0.0, self.serial_handshake_min_interval),
        )
        if not self.mqtt_tls:
            raise ValueError("MQTT TLS must be enabled for secure operation")
        if not self.mqtt_cafile:
            raise ValueError(
                "MQTT TLS is enabled but 'mqtt_cafile' is not configured"
            )
        if not self.serial_shared_secret:
            raise ValueError("serial_shared_secret must be configured")
        if len(self.serial_shared_secret) < MIN_SERIAL_SHARED_SECRET_LEN:
            raise ValueError(
                "serial_shared_secret must be at least %d bytes" %
                MIN_SERIAL_SHARED_SECRET_LEN
            )
        if self.serial_shared_secret == DEFAULT_SERIAL_SHARED_SECRET:
            raise ValueError(
                "serial_shared_secret placeholder is insecure"
            )
        object.__setattr__(
            self,
            "pending_pin_request_limit",
            max(1, self.pending_pin_request_limit),
        )
        unique_symbols = {byte for byte in self.serial_shared_secret}
        if len(unique_symbols) < 4:
            raise ValueError(
                "serial_shared_secret must contain at least "
                "four distinct bytes"
            )


def _load_raw_config() -> Dict[str, str]:
    try:
        uci_values = get_uci_config()
        if uci_values:
            return uci_values
    except Exception:
        # get_uci_config already logs, simply fall back to defaults
        pass
    return get_default_config()


def _to_bool(value: Optional[str]) -> bool:
    if value is None:
        return False
    return value.strip() in {"1", "true", "True", "yes", "on"}


def _optional_path(path: Optional[str]) -> Optional[str]:
    if not path:
        return None
    candidate = path.strip()
    return candidate or None


def _coerce_int(value: Optional[str], default: int) -> int:
    if value is None:
        return default
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _coerce_float(value: Optional[str], default: float) -> float:
    if value is None:
        return default
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def _resolve_watchdog_settings() -> Tuple[bool, float]:
    env_interval = os.environ.get("YUNBRIDGE_WATCHDOG_INTERVAL")
    if env_interval:
        try:
            interval = max(0.5, float(env_interval))
        except ValueError:
            interval = DEFAULT_WATCHDOG_INTERVAL
        return True, interval

    procd_raw = os.environ.get("PROCD_WATCHDOG")
    if procd_raw:
        try:
            procd_ms = max(0, int(procd_raw))
        except ValueError:
            procd_ms = 0
        if procd_ms > 0:
            heartbeat = max(1.0, procd_ms / 2000.0)
            return True, heartbeat

    return False, DEFAULT_WATCHDOG_INTERVAL


def _resolve_credentials_path(raw: Dict[str, str]) -> str:
    env_override = os.environ.get("YUNBRIDGE_CREDENTIALS_FILE")
    if env_override:
        trimmed = env_override.strip()
        if trimmed:
            return trimmed
    configured = raw.get("credentials_file")
    if configured:
        trimmed = configured.strip()
        if trimmed:
            return trimmed
    return DEFAULT_CREDENTIALS_FILE


def load_runtime_config() -> RuntimeConfig:
    """Load configuration from UCI/defaults and environment variables."""

    raw = _load_raw_config()

    def _get_int(key: str, default: int) -> int:
        return _coerce_int(raw.get(key), default)

    def _get_bool(key: str, default: bool) -> bool:
        value = raw.get(key)
        return _to_bool(value) if value is not None else default

    debug_logging = _to_bool(raw.get("debug"))
    if os.environ.get("YUNBRIDGE_DEBUG") == "1":
        debug_logging = True

    allowed_commands_raw = raw.get("allowed_commands", "")
    allowed_commands = normalise_allowed_commands(
        allowed_commands_raw.split()
    )

    watchdog_enabled, watchdog_interval = _resolve_watchdog_settings()

    mqtt_tls_value = raw.get("mqtt_tls")
    mqtt_tls = _to_bool(mqtt_tls_value) if mqtt_tls_value is not None else True

    credentials_path = _resolve_credentials_path(raw)
    credentials_map: Dict[str, str] = {}
    try:
        credentials_map = load_credentials_file(credentials_path)
    except FileNotFoundError:
        logger.warning(
            "Credentials file %s missing; falling back to inline config.",
            credentials_path,
        )
    except PermissionError as exc:
        raise RuntimeError(
            f"Insecure credentials file {credentials_path}: {exc}"
        ) from exc
    except Exception as exc:
        logger.warning(
            "Unable to load credentials file %s: %s",
            credentials_path,
            exc,
        )

    serial_secret_str = lookup_credential(
        (
            "YUNBRIDGE_SERIAL_SECRET",
            "SERIAL_SHARED_SECRET",
            "serial_shared_secret",
        ),
        credential_map=credentials_map,
        environ=os.environ,
        fallback=raw.get("serial_shared_secret") or "",
    )
    serial_secret_bytes = (
        serial_secret_str.encode("utf-8") if serial_secret_str else b""
    )

    spool_dir = os.environ.get("YUNBRIDGE_MQTT_SPOOL_DIR") or raw.get(
        "mqtt_spool_dir",
        DEFAULT_MQTT_SPOOL_DIR,
    )
    spool_dir = (spool_dir or DEFAULT_MQTT_SPOOL_DIR).strip()
    if not spool_dir:
        spool_dir = DEFAULT_MQTT_SPOOL_DIR

    mqtt_cafile = lookup_credential(
        (
            "YUNBRIDGE_MQTT_CAFILE",
            "MQTT_CAFILE",
            "mqtt_cafile",
        ),
        credential_map=credentials_map,
        environ=os.environ,
        fallback=raw.get("mqtt_cafile"),
    )
    mqtt_cafile = _optional_path(mqtt_cafile)
    if mqtt_cafile is None and mqtt_tls:
        mqtt_cafile = DEFAULT_MQTT_CAFILE

    mqtt_certfile = lookup_credential(
        (
            "YUNBRIDGE_MQTT_CERTFILE",
            "MQTT_CERTFILE",
            "mqtt_certfile",
        ),
        credential_map=credentials_map,
        environ=os.environ,
        fallback=raw.get("mqtt_certfile"),
    )
    mqtt_keyfile = lookup_credential(
        (
            "YUNBRIDGE_MQTT_KEYFILE",
            "MQTT_KEYFILE",
            "mqtt_keyfile",
        ),
        credential_map=credentials_map,
        environ=os.environ,
        fallback=raw.get("mqtt_keyfile"),
    )

    mqtt_user = lookup_credential(
        (
            "YUNBRIDGE_MQTT_USER",
            "MQTT_USERNAME",
            "mqtt_user",
        ),
        credential_map=credentials_map,
        environ=os.environ,
        fallback=raw.get("mqtt_user"),
    )
    mqtt_pass = lookup_credential(
        (
            "YUNBRIDGE_MQTT_PASS",
            "MQTT_PASSWORD",
            "mqtt_pass",
        ),
        credential_map=credentials_map,
        environ=os.environ,
        fallback=raw.get("mqtt_pass"),
    )

    topic_authorization = TopicAuthorization(
        file_read=_get_bool("mqtt_allow_file_read", True),
        file_write=_get_bool("mqtt_allow_file_write", True),
        file_remove=_get_bool("mqtt_allow_file_remove", True),
        datastore_get=_get_bool("mqtt_allow_datastore_get", True),
        datastore_put=_get_bool("mqtt_allow_datastore_put", True),
        mailbox_read=_get_bool("mqtt_allow_mailbox_read", True),
        mailbox_write=_get_bool("mqtt_allow_mailbox_write", True),
        shell_run=_get_bool("mqtt_allow_shell_run", True),
        shell_run_async=_get_bool("mqtt_allow_shell_run_async", True),
        shell_poll=_get_bool("mqtt_allow_shell_poll", True),
        shell_kill=_get_bool("mqtt_allow_shell_kill", True),
    )
    metrics_enabled = _get_bool("metrics_enabled", False)
    if os.environ.get("YUNBRIDGE_METRICS_ENABLED") == "1":
        metrics_enabled = True

    metrics_host = raw.get("metrics_host", DEFAULT_METRICS_HOST).strip()
    if not metrics_host:
        metrics_host = DEFAULT_METRICS_HOST
    env_metrics_host = os.environ.get("YUNBRIDGE_METRICS_HOST")
    if env_metrics_host:
        candidate_host = env_metrics_host.strip()
        if candidate_host:
            metrics_host = candidate_host

    metrics_port = _get_int("metrics_port", DEFAULT_METRICS_PORT)
    env_metrics_port = os.environ.get("YUNBRIDGE_METRICS_PORT")
    if env_metrics_port:
        try:
            metrics_port = int(env_metrics_port)
        except ValueError:
            metrics_port = DEFAULT_METRICS_PORT

    return RuntimeConfig(
        serial_port=raw.get("serial_port", DEFAULT_SERIAL_PORT),
        serial_baud=_get_int("serial_baud", DEFAULT_SERIAL_BAUD),
        mqtt_host=raw.get("mqtt_host", DEFAULT_MQTT_HOST),
        mqtt_port=_get_int("mqtt_port", DEFAULT_MQTT_PORT),
        mqtt_user=_optional_path(mqtt_user),
        mqtt_pass=_optional_path(mqtt_pass),
        mqtt_tls=mqtt_tls,
        mqtt_cafile=mqtt_cafile,
        mqtt_certfile=_optional_path(mqtt_certfile),
        mqtt_keyfile=_optional_path(mqtt_keyfile),
        mqtt_topic=raw.get("mqtt_topic", DEFAULT_MQTT_TOPIC),
        allowed_commands=allowed_commands,
        file_system_root=raw.get("file_system_root", DEFAULT_FILE_SYSTEM_ROOT),
        process_timeout=_get_int("process_timeout", DEFAULT_PROCESS_TIMEOUT),
        mqtt_queue_limit=max(
            1, _get_int("mqtt_queue_limit", DEFAULT_MQTT_QUEUE_LIMIT)
        ),
        reconnect_delay=_get_int("reconnect_delay", DEFAULT_RECONNECT_DELAY),
        status_interval=_get_int("status_interval", DEFAULT_STATUS_INTERVAL),
        debug_logging=debug_logging,
        console_queue_limit_bytes=_get_int(
            "console_queue_limit_bytes", DEFAULT_CONSOLE_QUEUE_LIMIT_BYTES
        ),
        mailbox_queue_limit=_get_int(
            "mailbox_queue_limit", DEFAULT_MAILBOX_QUEUE_LIMIT
        ),
        mailbox_queue_bytes_limit=_get_int(
            "mailbox_queue_bytes_limit", DEFAULT_MAILBOX_QUEUE_BYTES_LIMIT
        ),
        pending_pin_request_limit=_get_int(
            "pending_pin_request_limit",
            DEFAULT_PENDING_PIN_REQUESTS,
        ),
        serial_retry_timeout=_coerce_float(
            raw.get("serial_retry_timeout"), DEFAULT_SERIAL_RETRY_TIMEOUT
        ),
        serial_response_timeout=_coerce_float(
            raw.get("serial_response_timeout"), DEFAULT_SERIAL_RESPONSE_TIMEOUT
        ),
        serial_retry_attempts=max(
            1, _get_int("serial_retry_attempts", DEFAULT_SERIAL_RETRY_ATTEMPTS)
        ),
        serial_handshake_min_interval=max(
            0.0,
            _coerce_float(
                raw.get("serial_handshake_min_interval"),
                DEFAULT_SERIAL_HANDSHAKE_MIN_INTERVAL,
            ),
        ),
        watchdog_enabled=watchdog_enabled,
        watchdog_interval=watchdog_interval,
        topic_authorization=topic_authorization,
        serial_shared_secret=serial_secret_bytes,
        mqtt_spool_dir=spool_dir,
        process_max_output_bytes=max(
            1024,
            _get_int(
                "process_max_output_bytes",
                DEFAULT_PROCESS_MAX_OUTPUT_BYTES,
            ),
        ),
        process_max_concurrent=max(
            1,
            _get_int(
                "process_max_concurrent",
                DEFAULT_PROCESS_MAX_CONCURRENT,
            ),
        ),
        credentials_file=credentials_path,
        metrics_enabled=metrics_enabled,
        metrics_host=metrics_host,
        metrics_port=max(0, metrics_port),
    )
