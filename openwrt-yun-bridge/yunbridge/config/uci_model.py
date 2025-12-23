"""Dataclass-based normalisation for UCI key/value pairs."""

from __future__ import annotations

from collections.abc import Iterable as IterableABC, Mapping as MappingABC
from dataclasses import dataclass, field, fields
from typing import Any, Iterable, Mapping, cast

from yunbridge.common import parse_bool, parse_float, parse_int

from ..const import (
    DEFAULT_BRIDGE_HANDSHAKE_INTERVAL,
    DEFAULT_BRIDGE_SUMMARY_INTERVAL,
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
    DEFAULT_SERIAL_BAUD,
    DEFAULT_SERIAL_HANDSHAKE_FATAL_FAILURES,
    DEFAULT_SERIAL_HANDSHAKE_MIN_INTERVAL,
    DEFAULT_SERIAL_PORT,
    DEFAULT_SERIAL_RESPONSE_TIMEOUT,
    DEFAULT_SERIAL_RETRY_ATTEMPTS,
    DEFAULT_SERIAL_RETRY_TIMEOUT,
    DEFAULT_STATUS_INTERVAL,
)


def _stringify_option(value: Any) -> str:
    """Normalise nested option payloads into a flat string."""

    if isinstance(value, MappingABC):
        typed_map = cast(Mapping[str, Any], value)
        if "value" in typed_map:
            nested_value: Any = typed_map["value"]
            return _stringify_option(nested_value)
        if "values" in typed_map:
            nested_values: Any = typed_map["values"]
            if isinstance(nested_values, IterableABC) and not isinstance(
                nested_values,
                (str, bytes, bytearray),
            ):
                iterable_values = cast(Iterable[Any], nested_values)
                return " ".join(_stringify_option(item) for item in iterable_values)
            return _stringify_option(nested_values)

    if isinstance(value, IterableABC) and not isinstance(
        value,
        (str, bytes, bytearray),
    ):
        iterable_items = cast(Iterable[Any], value)
        return " ".join(_stringify_option(item) for item in iterable_items)

    if value is None:
        return ""
    return str(value)


@dataclass(slots=True)
class UciConfigModel:
    """Structured representation of UCI options with sane defaults and typed fields."""

    # MQTT Settings
    mqtt_host: str = DEFAULT_MQTT_HOST
    mqtt_port: int = DEFAULT_MQTT_PORT
    mqtt_tls: bool = True
    mqtt_cafile: str = DEFAULT_MQTT_CAFILE
    mqtt_certfile: str = ""
    mqtt_keyfile: str = ""
    mqtt_user: str = ""
    mqtt_pass: str = ""
    mqtt_topic: str = DEFAULT_MQTT_TOPIC
    mqtt_spool_dir: str = DEFAULT_MQTT_SPOOL_DIR
    mqtt_queue_limit: int = DEFAULT_MQTT_QUEUE_LIMIT

    # Serial Settings
    serial_port: str = DEFAULT_SERIAL_PORT
    serial_baud: int = DEFAULT_SERIAL_BAUD
    serial_shared_secret: str = ""
    serial_retry_timeout: float = DEFAULT_SERIAL_RETRY_TIMEOUT
    serial_response_timeout: float = DEFAULT_SERIAL_RESPONSE_TIMEOUT
    serial_retry_attempts: int = DEFAULT_SERIAL_RETRY_ATTEMPTS
    serial_handshake_min_interval: float = DEFAULT_SERIAL_HANDSHAKE_MIN_INTERVAL
    serial_handshake_fatal_failures: int = DEFAULT_SERIAL_HANDSHAKE_FATAL_FAILURES

    # General Settings
    debug: bool = False
    allowed_commands: str = ""
    file_system_root: str = DEFAULT_FILE_SYSTEM_ROOT
    process_timeout: float = DEFAULT_PROCESS_TIMEOUT
    process_max_output_bytes: int = DEFAULT_PROCESS_MAX_OUTPUT_BYTES
    process_max_concurrent: int = DEFAULT_PROCESS_MAX_CONCURRENT
    console_queue_limit_bytes: int = DEFAULT_CONSOLE_QUEUE_LIMIT_BYTES
    mailbox_queue_limit: int = DEFAULT_MAILBOX_QUEUE_LIMIT
    mailbox_queue_bytes_limit: int = DEFAULT_MAILBOX_QUEUE_BYTES_LIMIT
    pending_pin_request_limit: int = DEFAULT_PENDING_PIN_REQUESTS
    status_interval: float = DEFAULT_STATUS_INTERVAL
    bridge_summary_interval: float = DEFAULT_BRIDGE_SUMMARY_INTERVAL
    bridge_handshake_interval: float = DEFAULT_BRIDGE_HANDSHAKE_INTERVAL

    # Permissions (Defaults from '1' in original)
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

    # Metrics
    metrics_enabled: bool = False
    metrics_host: str = DEFAULT_METRICS_HOST
    metrics_port: int = DEFAULT_METRICS_PORT

    # Extras to preserve unknown keys
    extras: dict[str, str] = field(default_factory=lambda: cast(dict[str, str], {}))

    @classmethod
    def from_mapping(
        cls,
        mapping: Mapping[str, Any] | IterableABC[tuple[Any, Any]] | Any,
    ) -> UciConfigModel:
        """Create config model from a mapping, converting types appropriately."""
        known_fields = {f.name: f for f in fields(cls) if f.name != "extras"}
        init_args: dict[str, Any] = {}
        extras: dict[str, str] = {}

        items_iter: Iterable[tuple[Any, Any]]
        if isinstance(mapping, MappingABC):
            items_iter = cast(Iterable[tuple[Any, Any]], mapping.items())
        else:
            try:
                typed_iterable = cast(Iterable[tuple[Any, Any]], mapping)
                items_iter = dict(typed_iterable).items()
            except Exception:
                return cls()

        for key, value in items_iter:
            key_str = str(key)
            if key_str not in known_fields:
                extras[key_str] = _stringify_option(value)
                continue

            field_info = known_fields[key_str]
            target_type = field_info.type

            # Type conversion logic
            if target_type is bool:
                init_args[key_str] = parse_bool(value)
            elif target_type is int:
                # Get default from field or constant if possible for fallback
                default = (
                    field_info.default if isinstance(field_info.default, int) else 0
                )
                init_args[key_str] = parse_int(value, default)
            elif target_type is float:
                default = (
                    field_info.default
                    if isinstance(field_info.default, (int, float))
                    else 0.0
                )
                init_args[key_str] = parse_float(value, default)
            else:
                # Default to string with nested handling
                init_args[key_str] = _stringify_option(value)

        return cls(extras=extras, **init_args)

    def to_uci_dict(self) -> dict[str, str]:
        """Export configuration as a flat dictionary of strings for UCI."""
        output: dict[str, str] = {}

        for f in fields(self):
            if f.name == "extras":
                continue

            value = getattr(self, f.name)

            if isinstance(value, bool):
                output[f.name] = "1" if value else "0"
            elif value is None:
                output[f.name] = ""
            else:
                output[f.name] = str(value)

        # Merge extras
        output.update(self.extras)
        return output

    def as_dict(self) -> dict[str, str]:
        """Alias for legacy compatibility."""
        return self.to_uci_dict()

    @classmethod
    def defaults(cls) -> dict[str, str]:
        """Return the default configuration as a UCI dict."""
        return cls().to_uci_dict()


__all__ = [
    "UciConfigModel",
]
