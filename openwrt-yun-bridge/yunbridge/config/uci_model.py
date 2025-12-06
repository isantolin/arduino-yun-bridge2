"""attrs-based normalisation for UCI key/value pairs."""
from __future__ import annotations

from collections.abc import Iterable, Mapping
from typing import Any, Dict, Iterable as TypingIterable, Tuple, cast

from attrs import define, field, fields_dict

from ..const import (
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
    DEFAULT_BRIDGE_HANDSHAKE_INTERVAL,
    DEFAULT_BRIDGE_SUMMARY_INTERVAL,
    DEFAULT_SERIAL_BAUD,
    DEFAULT_SERIAL_HANDSHAKE_MIN_INTERVAL,
    DEFAULT_SERIAL_HANDSHAKE_FATAL_FAILURES,
    DEFAULT_SERIAL_PORT,
    DEFAULT_SERIAL_RESPONSE_TIMEOUT,
    DEFAULT_SERIAL_RETRY_ATTEMPTS,
    DEFAULT_SERIAL_RETRY_TIMEOUT,
    DEFAULT_STATUS_INTERVAL,
)


def _stringify_iterable(values: Iterable[Any]) -> str:
    parts: list[str] = []
    for item in values:
        parts.append(str(item))
    return " ".join(parts)


def _stringify_value(value: Any) -> str:
    attr_value = getattr(value, "value", None)
    if attr_value is not None and not isinstance(value, (str, bytes)):
        return _stringify_value(attr_value)

    if isinstance(value, Mapping):
        dict_value: Dict[str, Any] = {}
        mapping_items = cast(
            TypingIterable[Tuple[Any, Any]],
            value.items(),
        )
        for key, entry in mapping_items:
            dict_value[str(key)] = entry
        if "value" in dict_value:
            return _stringify_value(dict_value["value"])
        values_candidate: Any = dict_value.get("values")
        if isinstance(values_candidate, Iterable):
            iterable_values = cast(TypingIterable[Any], values_candidate)
            return _stringify_iterable(iterable_values)
        return _stringify_iterable(
            cast(TypingIterable[Any], dict_value.values())
        )

    if isinstance(value, (tuple, list, set)):
        iterable_value = cast(TypingIterable[Any], value)
        return _stringify_iterable(iterable_value)

    return str(value) if value is not None else ""


def _string_field(value: Any) -> str:
    return _stringify_value(value)


def _extras_converter(values: Mapping[str, Any] | None) -> Dict[str, str]:
    if not values:
        return {}
    items = cast(TypingIterable[Tuple[Any, Any]], values.items())
    return {str(key): _stringify_value(val) for key, val in items}


def _extras_factory() -> Dict[str, str]:
    return {}


@define(slots=True)
class UciConfigModel:
    """Structured representation of UCI options with sane defaults."""

    mqtt_host: str = field(default=DEFAULT_MQTT_HOST, converter=_string_field)
    mqtt_port: str = field(
        default=str(DEFAULT_MQTT_PORT),
        converter=_string_field,
    )
    mqtt_tls: str = field(default="1", converter=_string_field)
    mqtt_cafile: str = field(
        default=DEFAULT_MQTT_CAFILE,
        converter=_string_field,
    )
    mqtt_certfile: str = field(default="", converter=_string_field)
    mqtt_keyfile: str = field(default="", converter=_string_field)
    mqtt_user: str = field(default="", converter=_string_field)
    mqtt_pass: str = field(default="", converter=_string_field)
    mqtt_topic: str = field(
        default=DEFAULT_MQTT_TOPIC,
        converter=_string_field,
    )
    mqtt_spool_dir: str = field(
        default=DEFAULT_MQTT_SPOOL_DIR,
        converter=_string_field,
    )
    mqtt_queue_limit: str = field(
        default=str(DEFAULT_MQTT_QUEUE_LIMIT),
        converter=_string_field,
    )
    serial_port: str = field(
        default=DEFAULT_SERIAL_PORT,
        converter=_string_field,
    )
    serial_baud: str = field(
        default=str(DEFAULT_SERIAL_BAUD),
        converter=_string_field,
    )
    serial_shared_secret: str = field(default="", converter=_string_field)
    serial_retry_timeout: str = field(
        default=str(DEFAULT_SERIAL_RETRY_TIMEOUT),
        converter=_string_field,
    )
    serial_response_timeout: str = field(
        default=str(DEFAULT_SERIAL_RESPONSE_TIMEOUT),
        converter=_string_field,
    )
    serial_retry_attempts: str = field(
        default=str(DEFAULT_SERIAL_RETRY_ATTEMPTS),
        converter=_string_field,
    )
    serial_handshake_min_interval: str = field(
        default=str(DEFAULT_SERIAL_HANDSHAKE_MIN_INTERVAL),
        converter=_string_field,
    )
    serial_handshake_fatal_failures: str = field(
        default=str(DEFAULT_SERIAL_HANDSHAKE_FATAL_FAILURES),
        converter=_string_field,
    )
    debug: str = field(default="0", converter=_string_field)
    allowed_commands: str = field(default="", converter=_string_field)
    file_system_root: str = field(
        default=DEFAULT_FILE_SYSTEM_ROOT,
        converter=_string_field,
    )
    process_timeout: str = field(
        default=str(DEFAULT_PROCESS_TIMEOUT),
        converter=_string_field,
    )
    process_max_output_bytes: str = field(
        default=str(DEFAULT_PROCESS_MAX_OUTPUT_BYTES),
        converter=_string_field,
    )
    process_max_concurrent: str = field(
        default=str(DEFAULT_PROCESS_MAX_CONCURRENT),
        converter=_string_field,
    )
    console_queue_limit_bytes: str = field(
        default=str(DEFAULT_CONSOLE_QUEUE_LIMIT_BYTES),
        converter=_string_field,
    )
    mailbox_queue_limit: str = field(
        default=str(DEFAULT_MAILBOX_QUEUE_LIMIT),
        converter=_string_field,
    )
    mailbox_queue_bytes_limit: str = field(
        default=str(DEFAULT_MAILBOX_QUEUE_BYTES_LIMIT),
        converter=_string_field,
    )
    pending_pin_request_limit: str = field(
        default=str(DEFAULT_PENDING_PIN_REQUESTS),
        converter=_string_field,
    )
    reconnect_delay: str = field(
        default=str(DEFAULT_RECONNECT_DELAY),
        converter=_string_field,
    )
    status_interval: str = field(
        default=str(DEFAULT_STATUS_INTERVAL),
        converter=_string_field,
    )
    bridge_summary_interval: str = field(
        default=str(DEFAULT_BRIDGE_SUMMARY_INTERVAL),
        converter=_string_field,
    )
    bridge_handshake_interval: str = field(
        default=str(DEFAULT_BRIDGE_HANDSHAKE_INTERVAL),
        converter=_string_field,
    )
    mqtt_allow_file_read: str = field(default="1", converter=_string_field)
    mqtt_allow_file_write: str = field(default="1", converter=_string_field)
    mqtt_allow_file_remove: str = field(default="1", converter=_string_field)
    mqtt_allow_datastore_get: str = field(default="1", converter=_string_field)
    mqtt_allow_datastore_put: str = field(default="1", converter=_string_field)
    mqtt_allow_mailbox_read: str = field(default="1", converter=_string_field)
    mqtt_allow_mailbox_write: str = field(default="1", converter=_string_field)
    mqtt_allow_shell_run: str = field(default="1", converter=_string_field)
    mqtt_allow_shell_run_async: str = field(
        default="1",
        converter=_string_field,
    )
    mqtt_allow_shell_poll: str = field(default="1", converter=_string_field)
    mqtt_allow_shell_kill: str = field(default="1", converter=_string_field)
    metrics_enabled: str = field(default="0", converter=_string_field)
    metrics_host: str = field(
        default=DEFAULT_METRICS_HOST,
        converter=_string_field,
    )
    metrics_port: str = field(
        default=str(DEFAULT_METRICS_PORT),
        converter=_string_field,
    )
    extras: Dict[str, str] = field(
        factory=_extras_factory,
        converter=_extras_converter,
    )

    @classmethod
    def from_mapping(cls, mapping: Mapping[str, Any]) -> "UciConfigModel":
        known = cls._known_fields()
        kwargs: Dict[str, Any] = {}
        extras: Dict[str, Any] = {}
        for key, value in mapping.items():
            key_str = str(key)
            if key_str in known:
                kwargs[key_str] = value
            else:
                extras[key_str] = value
        return cls(extras=extras, **kwargs)

    def as_dict(self) -> Dict[str, str]:
        values = {name: getattr(self, name) for name in self._known_fields()}
        values.update(self.extras)
        return values

    @classmethod
    def defaults(cls) -> Dict[str, str]:
        return cls().as_dict()

    @classmethod
    def _known_fields(cls) -> set[str]:
        return {
            name
            for name in fields_dict(cls)
            if name != "extras"
        }


__all__ = [
    "UciConfigModel",
]
