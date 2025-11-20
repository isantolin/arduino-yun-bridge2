"""Settings loader for the Yun Bridge daemon.

This module centralises configuration loading from UCI and environment
variables so the rest of the code can depend on a strongly typed
RuntimeConfig instance.
"""
from __future__ import annotations

from dataclasses import dataclass, field
import os
from typing import Dict, Optional, Tuple

from ..common import (
    get_default_config,
    get_uci_config,
    normalise_allowed_commands,
)
from ..const import (
    DEFAULT_CONSOLE_QUEUE_LIMIT_BYTES,
    DEFAULT_FILE_SYSTEM_ROOT,
    DEFAULT_MAILBOX_QUEUE_BYTES_LIMIT,
    DEFAULT_MAILBOX_QUEUE_LIMIT,
    DEFAULT_MQTT_HOST,
    DEFAULT_MQTT_PORT,
    DEFAULT_MQTT_QUEUE_LIMIT,
    DEFAULT_MQTT_TOPIC,
    DEFAULT_PROCESS_TIMEOUT,
    DEFAULT_RECONNECT_DELAY,
    DEFAULT_SERIAL_BAUD,
    DEFAULT_SERIAL_PORT,
    DEFAULT_SERIAL_RESPONSE_TIMEOUT,
    DEFAULT_SERIAL_RETRY_ATTEMPTS,
    DEFAULT_SERIAL_RETRY_TIMEOUT,
    DEFAULT_STATUS_INTERVAL,
    DEFAULT_WATCHDOG_INTERVAL,
)
from ..policy import AllowedCommandPolicy


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
    serial_retry_timeout: float = DEFAULT_SERIAL_RETRY_TIMEOUT
    serial_response_timeout: float = DEFAULT_SERIAL_RESPONSE_TIMEOUT
    serial_retry_attempts: int = DEFAULT_SERIAL_RETRY_ATTEMPTS
    watchdog_enabled: bool = False
    watchdog_interval: float = DEFAULT_WATCHDOG_INTERVAL
    allowed_policy: AllowedCommandPolicy = field(init=False)

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


def load_runtime_config() -> RuntimeConfig:
    """Load configuration from UCI/defaults and environment variables."""

    raw = _load_raw_config()

    def _get_int(key: str, default: int) -> int:
        return _coerce_int(raw.get(key), default)

    debug_logging = _to_bool(raw.get("debug"))
    if os.environ.get("YUNBRIDGE_DEBUG") == "1":
        debug_logging = True

    allowed_commands_raw = raw.get("allowed_commands", "")
    allowed_commands = normalise_allowed_commands(
        allowed_commands_raw.split()
    )

    watchdog_enabled, watchdog_interval = _resolve_watchdog_settings()

    return RuntimeConfig(
        serial_port=raw.get("serial_port", DEFAULT_SERIAL_PORT),
        serial_baud=_get_int("serial_baud", DEFAULT_SERIAL_BAUD),
        mqtt_host=raw.get("mqtt_host", DEFAULT_MQTT_HOST),
        mqtt_port=_get_int("mqtt_port", DEFAULT_MQTT_PORT),
        mqtt_user=_optional_path(raw.get("mqtt_user")),
        mqtt_pass=_optional_path(raw.get("mqtt_pass")),
        mqtt_tls=_to_bool(raw.get("mqtt_tls")),
        mqtt_cafile=_optional_path(raw.get("mqtt_cafile")),
        mqtt_certfile=_optional_path(raw.get("mqtt_certfile")),
        mqtt_keyfile=_optional_path(raw.get("mqtt_keyfile")),
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
        serial_retry_timeout=_coerce_float(
            raw.get("serial_retry_timeout"), DEFAULT_SERIAL_RETRY_TIMEOUT
        ),
        serial_response_timeout=_coerce_float(
            raw.get("serial_response_timeout"), DEFAULT_SERIAL_RESPONSE_TIMEOUT
        ),
        serial_retry_attempts=max(
            1, _get_int("serial_retry_attempts", DEFAULT_SERIAL_RETRY_ATTEMPTS)
        ),
        watchdog_enabled=watchdog_enabled,
        watchdog_interval=watchdog_interval,
    )
