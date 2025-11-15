"""Settings loader for the Yun Bridge daemon.

This module centralises configuration loading from UCI and environment
variables so the rest of the code can depend on a strongly typed
RuntimeConfig instance.
"""
from __future__ import annotations

from dataclasses import dataclass
import os
from typing import Dict, List, Optional

from yunrpc.utils import get_default_config, get_uci_config


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
    allowed_commands: List[str]
    file_system_root: str
    process_timeout: int
    mqtt_queue_limit: int = 256
    reconnect_delay: int = 5
    status_interval: int = 5
    debug_logging: bool = False
    console_queue_limit_bytes: int = 16384
    mailbox_queue_limit: int = 64
    mailbox_queue_bytes_limit: int = 65536

    @property
    def tls_enabled(self) -> bool:
        return self.mqtt_tls and bool(self.mqtt_cafile)


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


def load_runtime_config() -> RuntimeConfig:
    """Load configuration from UCI/defaults and environment variables."""

    raw = _load_raw_config()

    def _get_int(key: str, default: int) -> int:
        return _coerce_int(raw.get(key), default)

    debug_logging = _to_bool(raw.get("debug"))
    if os.environ.get("YUNBRIDGE_DEBUG") == "1":
        debug_logging = True

    allowed_commands_raw = raw.get("allowed_commands", "")
    allowed_commands = [cmd for cmd in allowed_commands_raw.split() if cmd]

    return RuntimeConfig(
        serial_port=raw.get("serial_port", "/dev/ttyATH0"),
        serial_baud=_get_int("serial_baud", 115200),
        mqtt_host=raw.get("mqtt_host", "127.0.0.1"),
        mqtt_port=_get_int("mqtt_port", 1883),
        mqtt_user=_optional_path(raw.get("mqtt_user")),
        mqtt_pass=_optional_path(raw.get("mqtt_pass")),
        mqtt_tls=_to_bool(raw.get("mqtt_tls")),
        mqtt_cafile=_optional_path(raw.get("mqtt_cafile")),
        mqtt_certfile=_optional_path(raw.get("mqtt_certfile")),
        mqtt_keyfile=_optional_path(raw.get("mqtt_keyfile")),
        mqtt_topic=raw.get("mqtt_topic", "br"),
        allowed_commands=allowed_commands,
        file_system_root=raw.get("file_system_root", "/root/yun_files"),
        process_timeout=_get_int("process_timeout", 10),
        mqtt_queue_limit=max(1, _get_int("mqtt_queue_limit", 256)),
        reconnect_delay=_get_int("reconnect_delay", 5),
        status_interval=_get_int("status_interval", 5),
        debug_logging=debug_logging,
        console_queue_limit_bytes=_get_int(
            "console_queue_limit_bytes", 16384
        ),
        mailbox_queue_limit=_get_int("mailbox_queue_limit", 64),
        mailbox_queue_bytes_limit=_get_int(
            "mailbox_queue_bytes_limit", 65536
        ),
    )
