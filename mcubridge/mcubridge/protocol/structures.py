"""MCU Bridge Data Structures and Schemas. [SIL-2]

- RuntimeConfig: msgspec para configuración local.
- Protocolo: Protobuf-First con uso mandatorio de REFLEXIÓN (DESCRIPTOR).
- Erradicación de shims de msgspec, manteniendo la introspección de biblioteca.
"""

from __future__ import annotations

import asyncio
import enum
import functools
import re
from collections.abc import Iterable
from enum import IntEnum
from pathlib import Path
from typing import (
    Annotated,
    Any,
    Final,
    NamedTuple,
    cast,
    TypeAlias,
)

import msgspec
from google.protobuf.message import Message
from paho.mqtt.packettypes import PacketTypes
from paho.mqtt.properties import Properties

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

ProtobufMessage: TypeAlias = Message
PROTOBUF_CONTENT_TYPE: Final[str] = "application/x-protobuf"
_TOKEN_SEP: Final = re.compile(r"[,\s]+")


class QOSLevel(IntEnum):
    """MQTT Quality-of-Service levels."""

    QOS_0 = 0
    QOS_1 = 1
    QOS_2 = 2


UserProperty: TypeAlias = tuple[str, str]


class FlowEvent(str, enum.Enum):
    SENT = "sent"
    ACK = "ack"
    RETRY = "retry"
    FAILURE = "failure"


class PendingPinRequest(NamedTuple):
    pin: int
    reply_context: Any


class PendingCommand:
    """Book-keeping para un comando en vuelo (SIL-2)."""

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


@functools.lru_cache(maxsize=1)
def _get_action_lookup_map() -> dict[str, Any]:
    """[REFLEXIÓN] Mapeo dinámico de acciones desde Enums de protocolo."""
    from . import protocol as proto

    mapping: dict[str, Any] = {}
    for cls_name in [
        "FileAction",
        "ShellAction",
        "SystemAction",
        "ConsoleAction",
        "DatastoreAction",
        "MailboxAction",
        "PinAction",
        "SpiAction",
    ]:
        cls = getattr(proto, cls_name, None)
        if cls:
            for e in cls:
                mapping[e.value] = e
    return mapping


class TopicRoute(NamedTuple):
    raw: str
    prefix: str
    topic: Any
    segments: tuple[str, ...]

    @property
    def identifier(self) -> str:
        return self.segments[0] if self.segments else ""

    @property
    def action(self) -> Any:
        if not self.segments or "response" in self.segments or "value" in self.segments:
            return None
        return _get_action_lookup_map().get(self.segments[0])

    @property
    def remainder(self) -> tuple[str, ...]:
        return self.segments[1:] if len(self.segments) > 1 else ()


def is_command_allowed(policy: pb.AllowedCommandPolicy, command: str) -> bool:
    import fnmatch
    from mcubridge.config.const import ALLOWED_COMMAND_WILDCARD

    pieces = command.strip().split()
    return bool(
        pieces
        and (
            ALLOWED_COMMAND_WILDCARD in policy.entries
            or any(fnmatch.fnmatch(pieces[0].lower(), p) for p in policy.entries)
        )
    )


def create_allowed_policy(entries: Iterable[str]) -> pb.AllowedCommandPolicy:
    all_tokens: list[str] = []
    for c in entries:
        if c:
            all_tokens.extend(t for t in _TOKEN_SEP.split(c.strip().lower()) if t)
    items = set(all_tokens)
    return pb.AllowedCommandPolicy(entries=["*"] if "*" in items else sorted(list(items)))


@functools.lru_cache(maxsize=1)
def _get_topic_auth_mapping() -> dict[tuple[str, str], str]:
    """[DESCRIPTOR MAPPING] Inferencia dinámica de campos de autorización."""
    from . import protocol as proto
    from .topics import Topic

    mapping: dict[tuple[str, str], str] = {}
    fields = [f.name for f in pb.TopicAuthorization.DESCRIPTOR.fields]
    for field in fields:
        for t in Topic:
            prefix = t.name.lower()
            if field.startswith(f"{prefix}_"):
                suffix = field[len(prefix) + 1 :]
                action_cls = getattr(proto, f"{t.name.title()}Action", None)
                if t == Topic.SPI:
                    action_cls = proto.SpiAction
                if action_cls:
                    for act in action_cls:
                        if act.value == suffix or (suffix == "input" and act.value == "in"):
                            mapping[(t.value, act.value)] = field
                            break
    return mapping


def allows_topic(auth: pb.TopicAuthorization, topic: str, action: str) -> bool:
    """[REFLEXIÓN] Validación de tópicos usando mapeo por DESCRIPTOR."""
    field_name = _get_topic_auth_mapping().get((topic.lower(), action.lower()))
    return bool(getattr(auth, field_name)) if field_name else False


def iter_chunks(data: bytes, chunk_size: int) -> Iterable[bytes]:
    if not data:
        return
    v = memoryview(data)
    for i in range(0, len(data), chunk_size):
        yield bytes(v[i : i + chunk_size])


def create_queued_publish(
    topic_name: str,
    payload: bytes,
    retain: bool = False,
    qos: int = 1,
    content_type: str | None = None,
    message_expiry_interval: int | None = None,
    user_properties: Iterable[tuple[str, str]] = (),
) -> pb.MqttQueuedPublish:
    return pb.MqttQueuedPublish(
        topic_name=topic_name,
        payload=payload,
        retain=retain,
        qos=qos,
        content_type=content_type or "",
        message_expiry_interval=message_expiry_interval or 0,
        user_properties=[pb.UserProperty(key=k, value=v) for k, v in user_properties],
    )


def encode_queued_publish(message: pb.MqttQueuedPublish) -> bytes:
    return message.SerializeToString()


def decode_queued_publish(data: bytes) -> pb.MqttQueuedPublish:
    m = pb.MqttQueuedPublish()
    m.ParseFromString(data)
    return m


def resolve_mqtt_context(message: pb.MqttQueuedPublish, reply_context: Any | None) -> pb.MqttQueuedPublish:
    if not reply_context:
        return message
    if hasattr(reply_context, "properties") and reply_context.properties:
        props = reply_context.properties
        if hasattr(props, "CorrelationData") and props.CorrelationData:
            message.correlation_data = props.CorrelationData
        if hasattr(props, "ResponseTopic") and props.ResponseTopic:
            message.response_topic = props.ResponseTopic
    return message


def replace_mqtt_publish(message: pb.MqttQueuedPublish, **kwargs: Any) -> pb.MqttQueuedPublish:
    if "user_properties" in kwargs:
        props = kwargs.pop("user_properties")
        message.user_properties.extend([pb.UserProperty(key=k, value=v) for k, v in props])
    for k, v in kwargs.items():
        setattr(message, k, v)
    return message


def build_mqtt_properties(message: pb.MqttQueuedPublish) -> Properties:
    props = Properties(PacketTypes.PUBLISH)
    if message.content_type:
        props.ContentType = message.content_type
    if message.message_expiry_interval:
        props.MessageExpiryInterval = message.message_expiry_interval
    if message.topic_alias:
        props.TopicAlias = message.topic_alias
    if message.response_topic:
        props.ResponseTopic = message.response_topic
    if message.correlation_data:
        props.CorrelationData = message.correlation_data
    if message.user_properties:
        props.UserProperty = [(p.key, p.value) for p in message.user_properties]
    return props


class RuntimeConfig(msgspec.Struct, kw_only=True):
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
        if not self.mqtt_tls:
            return None
        import ssl
        from mcubridge.config.const import MQTT_TLS_MIN_VERSION

        try:
            ca_path = Path(self.mqtt_cafile) if self.mqtt_cafile else None
            context = ssl.create_default_context(
                ssl.Purpose.SERVER_AUTH, cafile=str(ca_path) if ca_path and ca_path.exists() else None
            )
            context.minimum_version = MQTT_TLS_MIN_VERSION
            if self.mqtt_tls_insecure:
                context.check_hostname = False
                context.verify_mode = ssl.CERT_NONE
            if self.mqtt_certfile and self.mqtt_keyfile:
                context.load_cert_chain(self.mqtt_certfile, self.mqtt_keyfile)
            return context
        except (OSError, ssl.SSLError, ValueError) as exc:
            raise RuntimeError(f"TLS setup failed: {exc}") from exc

    def __post_init__(self) -> None:
        from mcubridge.config.const import VOLATILE_STORAGE_PATHS

        self.allowed_policy = create_allowed_policy(self.allowed_commands)
        self.allowed_commands = tuple(self.allowed_policy.entries)
        if self.topic_authorization is None or isinstance(self.topic_authorization, dict):
            raw_auth = getattr(self, "topic_authorization", None)
            ta_dict = cast(dict[str, bool], raw_auth if isinstance(raw_auth, dict) else {})
            auth_pb = pb.TopicAuthorization()
            for f in [f.name for f in auth_pb.DESCRIPTOR.fields]:
                setattr(auth_pb, f, ta_dict.get(f, True))
            object.__setattr__(self, "topic_authorization", auth_pb)
        if self.serial_response_timeout < self.serial_retry_timeout * 2:
            raise ValueError("serial_response_timeout must be at least 2x serial_retry_timeout")
        if not self.serial_shared_secret:
            raise ValueError("serial_shared_secret must be configured")
        if not self.allow_non_tmp_paths:
            for p, n in [(self.mqtt_spool_dir, "mqtt_spool_dir"), (self.file_system_root, "file_system_root")]:
                if not any(p.startswith(v) for v in VOLATILE_STORAGE_PATHS):
                    raise ValueError(f"FLASH PROTECTION: {n} ({p}) must be in a volatile location")
