"""Dataclass-based normalisation for UCI key/value pairs."""
from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field, fields
from typing import Any, Dict, Iterable as TypingIterable, Tuple, cast

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


@dataclass(slots=True)
class UciConfigModel:
    """Structured representation of UCI options with sane defaults."""

    mqtt_host: str = DEFAULT_MQTT_HOST
    mqtt_port: str = str(DEFAULT_MQTT_PORT)
    mqtt_tls: str = "1"
    mqtt_cafile: str = DEFAULT_MQTT_CAFILE
    mqtt_certfile: str = ""
    mqtt_keyfile: str = ""
    mqtt_user: str = ""
    mqtt_pass: str = ""
    mqtt_topic: str = DEFAULT_MQTT_TOPIC
    mqtt_spool_dir: str = DEFAULT_MQTT_SPOOL_DIR
    mqtt_queue_limit: str = str(DEFAULT_MQTT_QUEUE_LIMIT)
    serial_port: str = DEFAULT_SERIAL_PORT
    serial_baud: str = str(DEFAULT_SERIAL_BAUD)
    serial_shared_secret: str = ""
    serial_retry_timeout: str = str(DEFAULT_SERIAL_RETRY_TIMEOUT)
    serial_response_timeout: str = str(DEFAULT_SERIAL_RESPONSE_TIMEOUT)
    serial_retry_attempts: str = str(DEFAULT_SERIAL_RETRY_ATTEMPTS)
    serial_handshake_min_interval: str = str(
        DEFAULT_SERIAL_HANDSHAKE_MIN_INTERVAL
    )
    serial_handshake_fatal_failures: str = str(
        DEFAULT_SERIAL_HANDSHAKE_FATAL_FAILURES
    )
    debug: str = "0"
    allowed_commands: str = ""
    file_system_root: str = DEFAULT_FILE_SYSTEM_ROOT
    process_timeout: str = str(DEFAULT_PROCESS_TIMEOUT)
    process_max_output_bytes: str = str(DEFAULT_PROCESS_MAX_OUTPUT_BYTES)
    process_max_concurrent: str = str(DEFAULT_PROCESS_MAX_CONCURRENT)
    console_queue_limit_bytes: str = str(DEFAULT_CONSOLE_QUEUE_LIMIT_BYTES)
    mailbox_queue_limit: str = str(DEFAULT_MAILBOX_QUEUE_LIMIT)
    mailbox_queue_bytes_limit: str = str(DEFAULT_MAILBOX_QUEUE_BYTES_LIMIT)
    pending_pin_request_limit: str = str(DEFAULT_PENDING_PIN_REQUESTS)
    reconnect_delay: str = str(DEFAULT_RECONNECT_DELAY)
    status_interval: str = str(DEFAULT_STATUS_INTERVAL)
    bridge_summary_interval: str = str(DEFAULT_BRIDGE_SUMMARY_INTERVAL)
    bridge_handshake_interval: str = str(DEFAULT_BRIDGE_HANDSHAKE_INTERVAL)
    mqtt_allow_file_read: str = "1"
    mqtt_allow_file_write: str = "1"
    mqtt_allow_file_remove: str = "1"
    mqtt_allow_datastore_get: str = "1"
    mqtt_allow_datastore_put: str = "1"
    mqtt_allow_mailbox_read: str = "1"
    mqtt_allow_mailbox_write: str = "1"
    mqtt_allow_shell_run: str = "1"
    mqtt_allow_shell_run_async: str = "1"
    mqtt_allow_shell_poll: str = "1"
    mqtt_allow_shell_kill: str = "1"
    metrics_enabled: str = "0"
    metrics_host: str = DEFAULT_METRICS_HOST
    metrics_port: str = str(DEFAULT_METRICS_PORT)
    extras: Dict[str, str] = field(default_factory=dict)

    def __post_init__(self) -> None:
        """Stringify all fields post-initialization."""
        # We iterate through all fields except 'extras' and ensure they are strings
        for f in fields(self):
            if f.name == "extras":
                continue
            value = getattr(self, f.name)
            setattr(self, f.name, _stringify_value(value))
        
        # Process extras
        new_extras = {}
        if self.extras:
            for k, v in self.extras.items():
                new_extras[str(k)] = _stringify_value(v)
        self.extras = new_extras

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
        return {f.name for f in fields(cls) if f.name != "extras"}


__all__ = [
    "UciConfigModel",
]