"""MCU Bridge Data Structures and Schemas.

SINGLE SOURCE OF TRUTH for all data structures.
Binary parsing uses stdlib struct; high-level schemas use Msgspec (SIL-2).
"""

from __future__ import annotations
from google.protobuf.message import Message as ProtobufMessage
from . import mcubridge_pb2 as pb
from .protocol import (
    DEFAULT_BAUDRATE,
    DEFAULT_CONSOLE_QUEUE_LIMIT_BYTES,
    DEFAULT_PROCESS_MAX_OUTPUT_BYTES,
    DEFAULT_RECONNECT_DELAY,
    DEFAULT_RETRY_LIMIT,
    DEFAULT_SAFE_BAUDRATE,
    DEFAULT_SERIAL_FALLBACK_THRESHOLD,
    DEFAULT_SERIAL_HANDSHAKE_FATAL_FAILURES,
    MQTT_DEFAULT_TOPIC_PREFIX,
    PROMETHEUS_PORT,
)


import asyncio
import enum
import functools
import re
from collections.abc import Iterable, Mapping
from enum import IntEnum
from dataclasses import dataclass
from pathlib import Path
from typing import (
    Annotated,
    Any,
    Final,
    NamedTuple,
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


class TopicRoute(NamedTuple):
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
# 2. Security and Policy Helpers (Direct Protobuf)
# =============================================================================


def is_command_allowed(policy: pb.AllowedCommandPolicy, command: str) -> bool:
    """Check if a shell/process command is allowed by the policy. [SIL-2]"""
    import fnmatch
    from mcubridge.config.const import ALLOWED_COMMAND_WILDCARD

    pieces = command.strip().split()
    if not pieces:
        return False
    return ALLOWED_COMMAND_WILDCARD in policy.entries or any(
        fnmatch.fnmatch(pieces[0].lower(), p) for p in policy.entries
    )


def create_allowed_policy(entries: Iterable[str]) -> pb.AllowedCommandPolicy:
    """Create a normalized AllowedCommandPolicy Protobuf message. [SIL-2]"""
    all_tokens: list[str] = []
    for c in entries:
        if not c:
            continue
        tokens = _TOKEN_SEP.split(c.strip().lower())
        all_tokens.extend(t for t in tokens if t)
    items: set[str] = set(all_tokens)
    normalised = ["*"] if "*" in items else sorted(list(items))
    return pb.AllowedCommandPolicy(entries=normalised)


@functools.lru_cache(maxsize=1)
def _get_topic_auth_mapping_v3() -> dict[tuple[str, str], str]:
    import mcubridge.protocol.protocol as proto
    from mcubridge.protocol.topics import Topic

    mapping: dict[tuple[str, str], str] = {}
    fields = [f.name for f in pb.TopicAuthorization.DESCRIPTOR.fields]
    for field in fields:
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


def allows_topic(auth: pb.TopicAuthorization, topic: str, action: str) -> bool:
    """Check if a specific topic/action combination is authorized. [SIL-2]"""
    mapping = _get_topic_auth_mapping_v3()
    field_name = mapping.get((topic.lower(), action.lower()))
    if field_name is not None:
        return bool(getattr(auth, field_name))
    return False


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

    allowed_policy: Any = None

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
    topic_authorization: Any = None

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
        self.allowed_policy = create_allowed_policy(self.allowed_commands)
        self.allowed_commands = tuple(self.allowed_policy.entries)

        if self.topic_authorization is None or isinstance(self.topic_authorization, dict):
            raw_auth = getattr(self, "topic_authorization", None)
            ta_dict = cast(dict[str, bool], raw_auth if isinstance(raw_auth, dict) else {})
            auth_pb = pb.TopicAuthorization()
            for field in [f.name for f in auth_pb.DESCRIPTOR.fields]:
                val = ta_dict.get(field, True)
                setattr(auth_pb, field, val)
            object.__setattr__(self, "topic_authorization", auth_pb)

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
    if hasattr(value, "_pb"):
        value = getattr(value, "_pb")

    if isinstance(value, ProtobufMessage):
        from google.protobuf.json_format import MessageToDict

        proto_fields = MessageToDict(value, preserving_proto_field_name=True)
        for key, nested in proto_fields.items():
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


def encode_structured_payload(payload: Mapping[str, Any]) -> bytes:
    message = pb.StructuredPayload()
    entries: list[pb.StructuredEntry] = []
    for key, value in payload.items():
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


# --- High-Level Structure ---


@dataclass
class PendingPinRequest:
    pin: int
    reply_context: Any | None = None


# --- MQTT Spool Helpers ---


class QOSLevel(IntEnum):
    """MQTT Quality-of-Service levels."""

    QOS_0 = 0
    QOS_1 = 1
    QOS_2 = 2


UserProperty = tuple[str, str]


def build_mqtt_properties(message: pb.MqttQueuedPublish) -> Properties:
    """Construct MQTT 5.0 properties object for aiomqtt/paho. [SIL-2]"""
    props = Properties(PacketTypes.PUBLISH)
    for field_desc in pb.MqttQueuedPublish.DESCRIPTOR.fields:
        field_name = field_desc.name
        if field_name in ("topic_name", "payload", "qos", "retain"):
            continue
        if field_desc.has_presence and not message.HasField(cast(Any, field_name)):
            continue
        val = getattr(message, field_name)
        if field_name == "user_properties":
            val = [(p.key, p.value) for p in message.user_properties]
            if not val:
                continue
        elif field_name == "subscription_identifier":
            val = list(message.subscription_identifier)
            if not val:
                continue
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
                        setattr(props, paho_name, val)
                    else:
                        setattr(props, paho_name, val)
    return props


def replace_mqtt_publish(message: pb.MqttQueuedPublish, **kwargs: Any) -> pb.MqttQueuedPublish:
    """Create a new MqttQueuedPublish with fields replaced."""
    new_pb = pb.MqttQueuedPublish()
    new_pb.CopyFrom(message)
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
    return new_pb


def resolve_mqtt_context(message: pb.MqttQueuedPublish, context: Any | None) -> pb.MqttQueuedPublish:
    """Resolve MQTT request-reply context into the publish message."""
    if context is None:
        return message

    props = getattr(context, "properties", None)
    updates: dict[str, Any] = {}

    if props:
        if rt := getattr(props, "ResponseTopic", None):
            updates["topic_name"] = str(rt)
        if cd := getattr(props, "CorrelationData", None):
            updates["correlation_data"] = bytes(cd)

    user_props = [(p.key, p.value) for p in message.user_properties]
    if req_topic := getattr(context, "topic", None):
        user_props.append(("bridge-request-topic", str(req_topic)))

    new_pb = pb.MqttQueuedPublish()
    new_pb.CopyFrom(message)
    if "topic_name" in updates:
        new_pb.topic_name = updates["topic_name"]
    if "correlation_data" in updates:
        new_pb.correlation_data = updates["correlation_data"]

    del new_pb.user_properties[:]
    for k, v in user_props:
        new_pb.user_properties.add(key=k, value=v)

    return new_pb


def create_queued_publish(
    topic_name: str,
    payload: bytes,
    content_type: str | None = None,
    message_expiry_interval: int | None = None,
    user_properties: Iterable[tuple[str, str]] = (),
) -> pb.MqttQueuedPublish:
    """Factory to create a MqttQueuedPublish message. [SIL-2]"""
    msg = pb.MqttQueuedPublish(
        topic_name=topic_name,
        payload=payload,
        content_type=content_type or "",
    )
    if message_expiry_interval is not None:
        msg.message_expiry_interval = message_expiry_interval
    for k, v in user_properties:
        msg.user_properties.add(key=k, value=v)
    return msg


def encode_queued_publish(message: pb.MqttQueuedPublish) -> bytes:
    """Helper to serialize MqttQueuedPublish to Protobuf binary format."""
    return message.SerializeToString()


def decode_queued_publish(data: bytes) -> pb.MqttQueuedPublish:
    """Helper to deserialize MqttQueuedPublish from Protobuf binary format."""
    pb_msg = pb.MqttQueuedPublish()
    pb_msg.ParseFromString(data)
    return pb_msg


# --- Serial Flow Structures ---


class PendingCommand:
    """Book-keeping for a tracked command in flight. [SIL-2]"""

    def __init__(
        self,
        command_id: int,
        expected_resp_ids: Iterable[int] = (),
        reply_topic: str | None = None,
        correlation_data: bytes | None = None,
    ) -> None:
        self.command_id = command_id
        self.expected_resp_ids = list(expected_resp_ids)
        self.reply_topic = reply_topic
        self.correlation_data = correlation_data
        self.attempts = 0
        self.success: bool | None = None
        self.failure_status: int | None = None
        self.ack_received = False
        self.completion = asyncio.Event()
        self.response_payload: bytes | ProtobufMessage | None = None

    def mark_success(self, payload: bytes | ProtobufMessage | None = None) -> None:
        self.response_payload = payload
        self.success = True
        if not self.completion.is_set():
            self.completion.set()

    def mark_failure(self, status: int | None) -> None:
        self.success = False
        if status is not None:
            self.failure_status = status
        if not self.completion.is_set():
            self.completion.set()
