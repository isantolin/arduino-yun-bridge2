"""MCU Bridge Data Structures and Schemas.

SINGLE SOURCE OF TRUTH for all data structures.
Binary parsing uses stdlib struct; high-level schemas use Msgspec (SIL-2).
"""

from __future__ import annotations
from google.protobuf.message import Message as ProtobufMessage
from . import mcubridge_pb2 as pb
from . import protocol
from .protocol import (
        DEFAULT_BAUDRATE,
        DEFAULT_CONSOLE_QUEUE_LIMIT_BYTES,
        DEFAULT_PROCESS_MAX_OUTPUT_BYTES,
        DEFAULT_RECONNECT_DELAY,
        DEFAULT_RETRY_LIMIT,
        DEFAULT_SAFE_BAUDRATE,
        DEFAULT_SERIAL_FALLBACK_THRESHOLD,
        DEFAULT_SERIAL_HANDSHAKE_FATAL_FAILURES,
        FILE_LARGE_WARNING_BYTES,
        MAX_SERIAL_FRAME_BYTES,
        MQTT_DEFAULT_TOPIC_PREFIX,
        MQTT_EXPIRY_CONSOLE,
        MQTT_EXPIRY_DATASTORE,
        MQTT_EXPIRY_DEFAULT,
        MQTT_EXPIRY_PIN,
        MQTT_EXPIRY_SHELL,
        PROMETHEUS_PORT,
        SYNC_TIMEOUT_MS,
)


import asyncio
import enum
import functools
import re
import time
from collections.abc import Iterable, Mapping
from enum import IntEnum
from pathlib import Path
from typing import (
    Annotated,
    Any,
    Final,
    cast,
)

import msgspec
from paho.mqtt.packettypes import PacketTypes
from paho.mqtt.properties import Properties


def iter_chunks(data: bytes, chunk_size: int) -> Iterable[bytes]:
    """Zero-copy chunking using memoryview for maximum throughput. [SIL-2]"""
    if not data:
        return
    view = memoryview(data)
    for i in range(0, len(data), chunk_size):
        yield bytes(view[i : i + chunk_size])


PROTOBUF_CONTENT_TYPE: Final[str] = "application/x-protobuf"

# [SIL-2] Declarative bitmask definition for MCU capabilities.
# This ensures atomic bit-level parsing/building via 's C-backed engine.
# Order matches the protocol specification (bit 0 to bit 15).


# [SIL-2] Compiled once at module load; reused across all AllowedCommandPolicy instances.
_TOKEN_SEP: Final = re.compile(r"[,\s]+")


class FlowEvent(str, enum.Enum):
    """Serial flow event types for typed dispatch."""

    SENT = "sent"
    ACK = "ack"
    RETRY = "retry"
    FAILURE = "failure"


@functools.lru_cache(maxsize=1)
def _get_action_lookup_map() -> dict[str, Any]:
    from .protocol import FileAction, ShellAction, SystemAction

    mapping: dict[str, Any] = {}
    for enum_cls in (FileAction, ShellAction, SystemAction):
        for e in enum_cls:
            mapping[e.value] = e
    return mapping


class TopicRoute(msgspec.Struct, frozen=True):
    """Parsed representation of an MQTT topic targeting the daemon."""

    raw: str
    prefix: str
    topic: Any  # Avoid circular import with .protocol.Topic
    segments: tuple[str, ...]

    @property
    def identifier(self) -> str:
        return self.segments[0] if self.segments else ""

    @property
    def action(self) -> Any:
        """Infer the service action from the first segment if applicable.
        Ignore segments that indicate a response flavor.
        """
        if not self.segments or "response" in self.segments or "value" in self.segments:
            return None
        val = self.segments[0]
        return _get_action_lookup_map().get(val, val)

    @property
    def remainder(self) -> tuple[str, ...]:
        return self.segments[1:] if len(self.segments) > 1 else ()


# =============================================================================
# 2. Security and Policy Structures (msgspec)
# =============================================================================


class AllowedCommandPolicy(msgspec.Struct, frozen=True):
    """Normalised allow-list for shell/process commands."""

    entries: tuple[str, ...] = ()

    @property
    def allow_all(self) -> bool:
        from mcubridge.config.const import ALLOWED_COMMAND_WILDCARD

        return ALLOWED_COMMAND_WILDCARD in self.entries

    def is_allowed(self, command: str) -> bool:
        import fnmatch

        pieces = command.strip().split()
        if not pieces:
            return False
        return self.allow_all or any(fnmatch.fnmatch(pieces[0].lower(), p) for p in self.entries)

    def __contains__(self, item: str) -> bool:
        return item.lower() in self.entries

    @classmethod
    def from_iterable(
        cls,
        entries: Iterable[str],
    ) -> AllowedCommandPolicy:
        """Return a deduplicated, lower-cased and sorted allow-list preserving wildcards."""
        all_tokens: list[str] = []
        for c in entries:
            if not c:
                continue
            tokens = _TOKEN_SEP.split(c.strip().lower())
            all_tokens.extend(t for t in tokens if t)

        items: set[str] = set(all_tokens)
        normalised = ("*",) if "*" in items else tuple(sorted(items))
        return cls(entries=normalised)


@functools.lru_cache(maxsize=1)
def _get_topic_auth_mapping() -> dict[tuple[str, str], str]:
    """Build and cache the (topic, action) → field-name map dynamically via reflection."""
    import mcubridge.protocol.protocol as proto
    from mcubridge.protocol.topics import Topic

    mapping: dict[tuple[str, str], str] = {}
    fields = [f.name for f in msgspec.structs.fields(TopicAuthorization)]

    for field in fields:
        if field.startswith("_"):
            continue
        for t in Topic:
            prefix = t.name.lower()
            if field.startswith(f"{prefix}_"):
                suffix = field[len(prefix) + 1 :]
                action_class_name = f"{t.name.title()}Action"
                if t == Topic.SPI:
                    action_class_name = "SpiAction"
                action_cls = getattr(proto, action_class_name, None)
                if action_cls is not None:
                    for act in action_cls:
                        if act.value == suffix or (suffix == "input" and act.value == "in"):
                            mapping[(t.value, act.value)] = field
                            break
    return mapping


class TopicAuthorization(msgspec.Struct, frozen=True):
    """Per-topic allow flags for MQTT-driven actions.

    Optimized for lookup speed using a pre-calculated frozenset of allowed (topic, action) tuples.
    """

    file_read: bool = True
    file_write: bool = True
    file_remove: bool = True
    datastore_get: bool = True
    datastore_put: bool = True
    mailbox_read: bool = True
    mailbox_write: bool = True
    shell_run_async: bool = True
    shell_poll: bool = True
    shell_kill: bool = True
    console_input: bool = True
    digital_write: bool = True
    digital_read: bool = True
    digital_mode: bool = True
    analog_write: bool = True
    analog_read: bool = True
    system_version: bool = True
    system_free_memory: bool = True
    system_bootloader: bool = True
    spi_begin: bool = True
    spi_end: bool = True
    spi_transfer: bool = True
    spi_config: bool = True

    # Cache for allowed permissions (not serialized)
    _allowed_cache: Final[frozenset[tuple[str, str]]] = frozenset()

    def __post_init__(self) -> None:
        """Build the optimized lookup cache using the module-level cached mapping."""
        mapping = _get_topic_auth_mapping()
        allowed = [k for k, attr in mapping.items() if getattr(self, attr)]
        object.__setattr__(self, "_allowed_cache", frozenset(allowed))

    def allows(self, topic: str, action: str) -> bool:
        """Check if action is allowed on topic. O(1) complexity."""
        return (topic.lower(), action.lower()) in self._allowed_cache


# =============================================================================
# 3. Runtime Configuration Structures (msgspec)
# =============================================================================


class RuntimeConfig(msgspec.Struct, kw_only=True):
    """Strongly typed configuration for the daemon."""

    # Imports moved inside __post_init__ or methods to avoid circularity
    # but we need constants for defaults.
    from mcubridge.config.const import (
        DEFAULT_ALLOW_NON_TMP_PATHS,
        DEFAULT_BRIDGE_HANDSHAKE_INTERVAL,
        DEFAULT_BRIDGE_SUMMARY_INTERVAL,
        DEFAULT_DEBUG,
        DEFAULT_FILE_STORAGE_QUOTA_BYTES,
        DEFAULT_FILE_SYSTEM_ROOT,
        DEFAULT_FILE_WRITE_MAX_BYTES,
        DEFAULT_MAILBOX_QUEUE_BYTES_LIMIT,
        DEFAULT_MAILBOX_QUEUE_LIMIT,
        DEFAULT_METRICS_ENABLED,
        DEFAULT_METRICS_HOST,
        DEFAULT_MQTT_CAFILE,
        DEFAULT_MQTT_HOST,
        DEFAULT_MQTT_PORT,
        DEFAULT_MQTT_QUEUE_LIMIT,
        DEFAULT_MQTT_SPOOL_DIR,
        DEFAULT_MQTT_TLS_INSECURE,
        DEFAULT_PENDING_PIN_REQUESTS,
        DEFAULT_PROCESS_MAX_CONCURRENT,
        DEFAULT_PROCESS_TIMEOUT,
        DEFAULT_SERIAL_HANDSHAKE_MIN_INTERVAL,
        DEFAULT_SERIAL_PORT,
        DEFAULT_SERIAL_RESPONSE_TIMEOUT,
        DEFAULT_SERIAL_RETRY_TIMEOUT,
        DEFAULT_SERIAL_SHARED_SECRET,
        DEFAULT_STATUS_INTERVAL,
        DEFAULT_WATCHDOG_INTERVAL,
        MIN_SERIAL_SHARED_SECRET_LEN,
    )

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

    # [SIL-2] Accept Any to allow raw strings from UCI/Tests, then coerce in __post_init__
    allowed_commands: Any = ()

    file_system_root: str = DEFAULT_FILE_SYSTEM_ROOT
    process_timeout: Annotated[int, msgspec.Meta(ge=1)] = DEFAULT_PROCESS_TIMEOUT

    mqtt_tls_insecure: bool = DEFAULT_MQTT_TLS_INSECURE
    file_write_max_bytes: Annotated[int, msgspec.Meta(ge=1)] = DEFAULT_FILE_WRITE_MAX_BYTES
    file_storage_quota_bytes: Annotated[int, msgspec.Meta(ge=1)] = DEFAULT_FILE_STORAGE_QUOTA_BYTES

    allowed_policy: AllowedCommandPolicy | None = None

    mqtt_queue_limit: Annotated[int, msgspec.Meta(ge=0)] = DEFAULT_MQTT_QUEUE_LIMIT
    reconnect_delay: Annotated[int, msgspec.Meta(ge=1)] = DEFAULT_RECONNECT_DELAY
    status_interval: Annotated[int, msgspec.Meta(ge=1)] = DEFAULT_STATUS_INTERVAL
    debug: bool = DEFAULT_DEBUG
    console_queue_limit_bytes: Annotated[int, msgspec.Meta(ge=1)] = DEFAULT_CONSOLE_QUEUE_LIMIT_BYTES
    mailbox_queue_limit: Annotated[int, msgspec.Meta(ge=1)] = DEFAULT_MAILBOX_QUEUE_LIMIT
    mailbox_queue_bytes_limit: Annotated[int, msgspec.Meta(ge=1)] = DEFAULT_MAILBOX_QUEUE_BYTES_LIMIT
    pending_pin_request_limit: Annotated[int, msgspec.Meta(ge=1)] = DEFAULT_PENDING_PIN_REQUESTS
    serial_retry_timeout: Annotated[float, msgspec.Meta(ge=0.01, le=30.0)] = DEFAULT_SERIAL_RETRY_TIMEOUT
    serial_response_timeout: Annotated[float, msgspec.Meta(ge=0.02, le=120.0)] = DEFAULT_SERIAL_RESPONSE_TIMEOUT
    serial_retry_attempts: Annotated[int, msgspec.Meta(ge=0)] = DEFAULT_RETRY_LIMIT
    serial_fallback_threshold: Annotated[int, msgspec.Meta(ge=1)] = DEFAULT_SERIAL_FALLBACK_THRESHOLD
    serial_handshake_min_interval: Annotated[float, msgspec.Meta(ge=0.0, le=30.0)] = (
        DEFAULT_SERIAL_HANDSHAKE_MIN_INTERVAL
    )
    serial_handshake_fatal_failures: Annotated[int, msgspec.Meta(ge=1)] = DEFAULT_SERIAL_HANDSHAKE_FATAL_FAILURES
    mqtt_enabled: bool = True
    watchdog_enabled: bool = True
    watchdog_interval: Annotated[float, msgspec.Meta(ge=0.1, le=60.0)] = DEFAULT_WATCHDOG_INTERVAL
    topic_authorization: TopicAuthorization | None = None

    # [SIL-2] Security: Accept Any to allow raw strings from UCI/Tests,
    # then coerce to bytes in __post_init__ to avoid msgspec base64 errors.
    serial_shared_secret: Any = DEFAULT_SERIAL_SHARED_SECRET

    mqtt_spool_dir: str = DEFAULT_MQTT_SPOOL_DIR
    process_max_output_bytes: Annotated[int, msgspec.Meta(ge=1)] = DEFAULT_PROCESS_MAX_OUTPUT_BYTES
    process_max_concurrent: Annotated[int, msgspec.Meta(ge=1)] = DEFAULT_PROCESS_MAX_CONCURRENT
    metrics_enabled: bool = DEFAULT_METRICS_ENABLED
    metrics_host: str = DEFAULT_METRICS_HOST
    metrics_port: Annotated[int, msgspec.Meta(ge=1, le=65535)] = PROMETHEUS_PORT
    bridge_summary_interval: Annotated[float, msgspec.Meta(ge=0.0)] = DEFAULT_BRIDGE_SUMMARY_INTERVAL
    bridge_handshake_interval: Annotated[float, msgspec.Meta(ge=0.0)] = DEFAULT_BRIDGE_HANDSHAKE_INTERVAL
    allow_non_tmp_paths: bool = DEFAULT_ALLOW_NON_TMP_PATHS

    def get_ssl_context(self) -> Any | None:
        """Create an ssl.SSLContext based on the current configuration (SIL-2)."""
        if not self.mqtt_tls:
            return None

        import ssl
        from mcubridge.config.const import MQTT_TLS_MIN_VERSION

        try:
            if self.mqtt_cafile:
                ca_path = Path(self.mqtt_cafile)
                if not ca_path.exists():
                    raise RuntimeError(f"MQTT TLS CA file missing: {self.mqtt_cafile}")
                context = ssl.create_default_context(ssl.Purpose.SERVER_AUTH, cafile=str(ca_path))
            else:
                context = ssl.create_default_context(ssl.Purpose.SERVER_AUTH)

            context.minimum_version = MQTT_TLS_MIN_VERSION

            if self.mqtt_tls_insecure:
                context.check_hostname = False
                context.verify_mode = ssl.CERT_NONE

            if self.mqtt_certfile or self.mqtt_keyfile:
                if not (self.mqtt_certfile and self.mqtt_keyfile):
                    raise ValueError("Both mqtt_certfile and mqtt_keyfile must be provided for mTLS.")
                context.load_cert_chain(self.mqtt_certfile, self.mqtt_keyfile)

            return context
        except (OSError, ssl.SSLError, ValueError) as exc:
            raise RuntimeError(f"TLS setup failed: {exc}") from exc

    @property
    def tls_enabled(self) -> bool:
        return self.mqtt_tls

    def __post_init__(self) -> None:
        from mcubridge.config.const import (
            DEFAULT_SERIAL_SHARED_SECRET,
            VOLATILE_STORAGE_PATHS,
        )

        # [SIL-2] Semantic Policy Derivation
        self.allowed_policy = AllowedCommandPolicy.from_iterable(self.allowed_commands)
        self.allowed_commands = self.allowed_policy.entries if self.allowed_policy else ()

        if self.topic_authorization is None or isinstance(self.topic_authorization, dict):
            self.topic_authorization = (
                msgspec.convert(self.topic_authorization, TopicAuthorization)
                if self.topic_authorization
                else TopicAuthorization()
            )

        # [SIL-2] Strict Semantic Validations
        if not self.mqtt_topic or not any(filter(None, self.mqtt_topic.split("/"))):
            raise ValueError("mqtt_topic must contain at least one segment")

        if self.serial_response_timeout < self.serial_retry_timeout * 2:
            raise ValueError("serial_response_timeout must be at least 2x serial_retry_timeout")

        if self.watchdog_enabled and self.watchdog_interval < 0.5:
            raise ValueError("watchdog_interval must be >= 0.5s when enabled")

        if not self.serial_shared_secret:
            raise ValueError("serial_shared_secret must be configured")

        if self.serial_shared_secret == b"changeme123":
            raise ValueError("serial_shared_secret placeholder is insecure")

        # Unique symbol check for minimum entropy
        if isinstance(self.serial_shared_secret, bytes):
            unique_symbols = {byte for byte in self.serial_shared_secret}
            if len(unique_symbols) < 4 and self.serial_shared_secret != DEFAULT_SERIAL_SHARED_SECRET:
                raise ValueError("serial_shared_secret must contain at least four distinct bytes")

        # Logic-based cross-field validations
        if self.file_storage_quota_bytes < self.file_write_max_bytes:
            raise ValueError("file_storage_quota_bytes must be greater than or equal to file_write_max_bytes")

        if self.mailbox_queue_bytes_limit < self.mailbox_queue_limit:
            raise ValueError("mailbox_queue_bytes_limit must be greater than or equal to mailbox_queue_limit")

        # [SIL-2] Flash Protection: Spooling must ALWAYS be in volatile RAM.
        if not self.allow_non_tmp_paths:
            if not any(self.mqtt_spool_dir.startswith(p) for p in VOLATILE_STORAGE_PATHS):
                msg = f"FLASH PROTECTION: mqtt_spool_dir ({self.mqtt_spool_dir}) must be in a volatile location"
                raise ValueError(msg)

        if not self.allow_non_tmp_paths:
            if not any(self.file_system_root.startswith(p) for p in VOLATILE_STORAGE_PATHS):
                raise ValueError(
                    f"FLASH PROTECTION: file_system_root ({self.file_system_root}) must be in a volatile location"
                )


# =============================================================================
# 3. Operational Structures
# =============================================================================


def _flatten_structured_value(
    key_prefix: str,
    value: Any,
    entries: list[pb.StructuredEntry],
) -> None:
    if isinstance(value, ProtobufMessage):
        from google.protobuf.json_format import MessageToDict

        proto_fields = MessageToDict(value, preserving_proto_field_name=True)
        for key, nested in proto_fields.items():
            _flatten_structured_value(f"{key_prefix}.{key}" if key_prefix else key, nested, entries)
        return
    if isinstance(value, msgspec.Struct):
        struct_fields = msgspec.structs.asdict(value)
        for key, nested in struct_fields.items():
            _flatten_structured_value(f"{key_prefix}.{key}" if key_prefix else key, nested, entries)
        return
    if isinstance(value, (list, tuple)):
        nested: Any
        for i, nested in enumerate(cast(list[Any] | tuple[Any, ...], value)):
            _flatten_structured_value(f"{key_prefix}.{i}" if key_prefix else str(i), nested, entries)
        return
    if isinstance(value, Mapping):
        mapped_value = cast(Mapping[str, Any], value)
        for key, nested in mapped_value.items():
            key_name = str(key)
            _flatten_structured_value(f"{key_prefix}.{key_name}" if key_prefix else key_name, nested, entries)
        return

    entry = pb.StructuredEntry(key=key_prefix)
    if value is None:
        entry.null_value = True
    elif isinstance(value, bytes):
        entry.bytes_value = value
    elif isinstance(value, str):
        entry.string_value = value
    elif isinstance(value, bool):
        entry.bool_value = value
    elif isinstance(value, enum.IntEnum):
        entry.int_value = int(value)
    elif isinstance(value, int):
        entry.int_value = value
    elif isinstance(value, float):
        entry.float_value = value
    else:
        raise TypeError(f"Unsupported structured payload value for '{key_prefix}': {type(value)!r}")
    entries.append(entry)


def encode_structured_payload(payload: Mapping[str, Any] | msgspec.Struct) -> bytes:
    message = pb.StructuredPayload()
    source: Mapping[str, Any] = msgspec.structs.asdict(payload) if isinstance(payload, msgspec.Struct) else payload
    entries: list[pb.StructuredEntry] = []
    for key, value in source.items():
        _flatten_structured_value(str(key), value, entries)
    message.entries.extend(entries)
    return message.SerializeToString()


def _entry_value(entry: pb.StructuredEntry) -> Any:
    match entry.WhichOneof("value"):
        case "string_value":
            return entry.string_value
        case "bytes_value":
            return bytes(entry.bytes_value)
        case "bool_value":
            return entry.bool_value
        case "int_value":
            return entry.int_value
        case "float_value":
            return entry.float_value
        case "null_value":
            return None
        case _:
            raise ValueError(f"StructuredEntry '{entry.key}' missing value")


def decode_structured_payload(data: bytes) -> dict[str, Any]:
    message = pb.StructuredPayload()
    message.ParseFromString(data)
    decoded: dict[str, Any] = {}
    for entry in message.entries:
        cursor: dict[str, Any] = decoded
        parts = entry.key.split(".")
        for part in parts[:-1]:
            next_cursor_obj = cursor.get(part)
            if not isinstance(next_cursor_obj, dict):
                next_cursor: dict[str, Any] = {}
                cursor[part] = next_cursor
            else:
                next_cursor = cast(dict[str, Any], next_cursor_obj)
            cursor = next_cursor
        cursor[parts[-1]] = _entry_value(entry)
    return decoded


# --- Binary Protocol Packets ---


class PayloadValidationError(ValueError):
    """Raised when an inbound MQTT payload cannot be validated."""

    def __init__(self, message: str) -> None:
        super().__init__(message)
        self.message = message


# --- High-Level Structure (Msgspec Only) ---


class PendingPinRequest(msgspec.Struct):
    """Pending pin read request."""

    pin: int
    reply_context: Any | None = None  # Message | None


class ServiceHealth(msgspec.Struct, frozen=True):
    name: str
    status: str
    restarts: int
    last_failure_unix: float
    last_exception: str | None = None


# --- MQTT Spool Structures ---


class QOSLevel(IntEnum):
    """MQTT Quality-of-Service levels."""

    QOS_0 = 0
    QOS_1 = 1
    QOS_2 = 2


UserProperty = tuple[str, str]


def build_mqtt_properties(message: QueuedPublish) -> Properties:
    """Construct MQTT 5.0 properties object for aiomqtt/paho. [SIL-2]"""
    props = Properties(PacketTypes.PUBLISH)
    for field_desc in pb.MqttQueuedPublish.DESCRIPTOR.fields:
        field_name = field_desc.name
        if field_name in ("topic_name", "payload", "qos", "retain"):
            continue
        val = getattr(message, field_name)
        if val is None:
            continue
        paho_name = "".join(part.capitalize() for part in field_name.split("_"))
        if paho_name == "UserProperties":
            paho_name = "UserProperty"
        ident = props.getIdentFromName(paho_name)
        if ident != -1:
            properties_dict = cast(dict[int, Any], props.properties)
            if ident in properties_dict:
                if PacketTypes.PUBLISH in properties_dict[ident][1]:
                    if paho_name in ("UserProperty", "SubscriptionIdentifier"):
                        setattr(props, paho_name, list(val))
                    else:
                        setattr(props, paho_name, val)
    return props


class QueuedPublish:
    """Serializable MQTT publish packet helper to eliminate schema duplication.

    This class wraps pb.MqttQueuedPublish directly, avoiding duplication of fields.
    """

    __slots__ = ("_pb",)

    def __init__(
        self,
        topic_name: str,
        payload: bytes,
        qos: int = 0,
        retain: bool = False,
        content_type: str | None = None,
        payload_format_indicator: int | None = None,
        message_expiry_interval: int | None = None,
        response_topic: str | None = None,
        correlation_data: bytes | None = None,
        user_properties: Iterable[tuple[str, str]] = (),
        subscription_identifier: Iterable[int] | None = None,
        topic_alias: int | None = None,
    ) -> None:
        self._pb = pb.MqttQueuedPublish(
            topic_name=topic_name,
            payload=payload,
            qos=qos,
            retain=retain,
        )
        if content_type is not None:
            self._pb.content_type = content_type
        if payload_format_indicator is not None:
            self._pb.payload_format_indicator = payload_format_indicator
        if message_expiry_interval is not None:
            self._pb.message_expiry_interval = message_expiry_interval
        if response_topic is not None:
            self._pb.response_topic = response_topic
        if correlation_data is not None:
            self._pb.correlation_data = correlation_data
        for k, v in user_properties:
            self._pb.user_properties.add(key=k, value=v)
        if subscription_identifier is not None:
            self._pb.subscription_identifier.extend(subscription_identifier)
        if topic_alias is not None:
            self._pb.topic_alias = topic_alias

    def __getattr__(self, name: str) -> Any:
        if name == "user_properties":
            return tuple((p.key, p.value) for p in self._pb.user_properties)
        if name == "subscription_identifier":
            return tuple(self._pb.subscription_identifier) if self._pb.subscription_identifier else None

        # Check optional fields presence
        if name in (
            "content_type",
            "payload_format_indicator",
            "message_expiry_interval",
            "response_topic",
            "correlation_data",
            "topic_alias",
        ):
            if self._pb.HasField(name):
                return getattr(self._pb, name)
            return None

        try:
            return getattr(self._pb, name)
        except AttributeError:
            raise AttributeError(f"'QueuedPublish' object has no attribute '{name}'")

    def __eq__(self, other: Any) -> bool:
        if not isinstance(other, QueuedPublish):
            return NotImplemented
        return self._pb.SerializeToString() == other._pb.SerializeToString()

    def __hash__(self) -> int:
        return hash(self._pb.SerializeToString())

    def __repr__(self) -> str:
        return f"QueuedPublish(topic_name={self.topic_name!r}, qos={self.qos}, retain={self.retain})"

    def replace(self, **kwargs: Any) -> QueuedPublish:
        """Create a new QueuedPublish with fields replaced."""
        new_pb = pb.MqttQueuedPublish()
        new_pb.CopyFrom(self._pb)
        for k, v in kwargs.items():
            if k == "user_properties":
                del new_pb.user_properties[:]
                for pk, pv in v:
                    new_pb.user_properties.add(key=pk, value=pv)
            elif k == "subscription_identifier":
                del new_pb.subscription_identifier[:]
                if v is not None:
                    new_pb.subscription_identifier.extend(v)
            else:
                setattr(new_pb, k, v)
        instance = QueuedPublish.__new__(QueuedPublish)
        instance._pb = new_pb
        return instance

    def resolve_context(self, context: Any | None) -> QueuedPublish:
        """Resolve MQTT request-reply context into the publish packet."""
        if context is None:
            return self

        props = getattr(context, "properties", None)
        updates: dict[str, Any] = {}

        if props:
            if rt := getattr(props, "ResponseTopic", None):
                updates["topic_name"] = str(rt)
            if cd := getattr(props, "CorrelationData", None):
                updates["correlation_data"] = bytes(cd)

        user_props = list(self.user_properties)
        if req_topic := getattr(context, "topic", None):
            user_props.append(("bridge-request-topic", str(req_topic)))

        new_pb = pb.MqttQueuedPublish()
        new_pb.CopyFrom(self._pb)
        if "topic_name" in updates:
            new_pb.topic_name = updates["topic_name"]
        if "correlation_data" in updates:
            new_pb.correlation_data = updates["correlation_data"]

        del new_pb.user_properties[:]
        for k, v in user_props:
            new_pb.user_properties.add(key=k, value=v)

        instance = QueuedPublish.__new__(QueuedPublish)
        instance._pb = new_pb
        return instance

    def to_protobuf(self) -> bytes:
        return self._pb.SerializeToString()

    @classmethod
    def from_protobuf(cls, data: bytes) -> QueuedPublish:
        pb_msg = pb.MqttQueuedPublish()
        pb_msg.ParseFromString(data)
        instance = cls.__new__(cls)
        instance._pb = pb_msg
        return instance


def encode_queued_publish(message: QueuedPublish) -> bytes:
    """Helper to serialize QueuedPublish to Protobuf binary format."""
    return message.to_protobuf()


def decode_queued_publish(data: bytes) -> QueuedPublish:
    """Helper to deserialize QueuedPublish from Protobuf binary format."""
    return QueuedPublish.from_protobuf(data)


# --- Process Service Structures ---


# --- Serial Flow Structures ---


class PendingCommand(msgspec.Struct):
    """Book-keeping for a tracked command in flight."""

    command_id: int
    expected_resp_ids: set[int] = msgspec.field(default_factory=lambda: cast(set[int], set()))
    completion: asyncio.Event = msgspec.field(default_factory=asyncio.Event)
    attempts: int = 0
    success: bool | None = None
    failure_status: int | None = None
    ack_received: bool = False
    reply_topic: str | None = None
    correlation_data: bytes | None = None
    response_payload: bytes | ProtobufMessage | None = None

    def mark_success(self, payload: bytes | ProtobufMessage | None = None) -> None:
        self.response_payload = payload
        self.success = True
        if not self.completion.is_set():
            self.completion.set()

    def mark_failure(self, status: int | None) -> None:
        self.success = False
        self.failure_status = status
        if not self.completion.is_set():
            self.completion.set()


# --- Status Structures ---


class SerialThroughputStats(msgspec.Struct):
    """Serial link throughput counters."""

    bytes_sent: int = 0
    bytes_received: int = 0
    frames_sent: int = 0
    frames_received: int = 0
    last_tx_unix: float = 0.0
    last_rx_unix: float = 0.0

    def record_tx(self, nbytes: int) -> None:
        self.bytes_sent += nbytes
        self.frames_sent += 1
        self.last_tx_unix = time.time()

    def record_rx(self, nbytes: int) -> None:
        self.bytes_received += nbytes
        self.frames_received += 1
        self.last_rx_unix = time.time()


class ProcessStats(msgspec.Struct):
    """Resource usage statistics for a single process."""

    name: str
    cpu_percent: Annotated[float, msgspec.Meta(ge=0.0)]
    memory_rss_bytes: Annotated[int, msgspec.Meta(ge=0)]
