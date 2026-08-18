"""MCU Bridge Data Structures and Schemas.

SINGLE SOURCE OF TRUTH for all data structures.
Binary parsing uses stdlib struct; high-level schemas use Protobuf (SIL-2) [TESTED].
"""

from __future__ import annotations

import asyncio
import fnmatch
import functools
import itertools
import re2
import ssl
from collections.abc import Iterable
from dataclasses import dataclass, field
from itertools import batched
from pathlib import Path
from typing import (
    Any,
    Final,
)

import protovalidate
from buf.validate.validate_pb2 import Violation as ProtovalidateViolation
from google.protobuf.message import Message as ProtobufMessage

from . import mcubridge_pb2 as pb
from mcubridge.config.const import ALLOWED_COMMAND_WILDCARD, CLOUD_TLS_MIN_VERSION


def iter_chunks(data: bytes, chunk_size: int) -> Iterable[bytes]:
    """Chunk bytes into fixed-size pieces. [SIL-2] Delegates to itertools.batched."""
    return (bytes(chunk) for chunk in batched(data, chunk_size))


PROTOBUF_CONTENT_TYPE: Final[str] = "application/x-protobuf"

# [SIL-2] Compiled once at module load with google-re2 (linear-time, ReDoS-safe)
_TOKEN_SEP: Final = re2.compile(r"[,\s]+")


@functools.lru_cache(maxsize=1)
def _get_action_lookup_map() -> dict[str, Any]:
    from .protocol import FileAction, ShellAction, SystemAction

    return {e.value: e for e in itertools.chain(FileAction, ShellAction, SystemAction)}


@dataclass(slots=True, frozen=True)
class TopicRoute:
    """Parsed representation of an CLOUD topic targeting the daemon."""

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
    pieces = command.strip().split()
    if not pieces:
        return False
    return ALLOWED_COMMAND_WILDCARD in policy.entries or any(
        fnmatch.fnmatch(pieces[0].lower(), p) for p in policy.entries
    )


def create_allowed_policy(entries: Iterable[str]) -> pb.AllowedCommandPolicy:
    """Create a normalized AllowedCommandPolicy Protobuf message. [SIL-2]"""
    items = {item for c in entries if c for item in _TOKEN_SEP.split(c.strip().lower()) if item}
    normalised = ["*"] if "*" in items else sorted(items)
    return pb.AllowedCommandPolicy(entries=normalised)


_TOPIC_AUTH_MAP: Final[dict[tuple[str, str], str]] = {
    ("a", "read"): "analog_read",
    ("a", "write"): "analog_write",
    ("console", "in"): "console_input",
    ("d", "mode"): "digital_mode",
    ("d", "read"): "digital_read",
    ("d", "write"): "digital_write",
    ("datastore", "get"): "datastore_get",
    ("datastore", "put"): "datastore_put",
    ("file", "read"): "file_read",
    ("file", "remove"): "file_remove",
    ("file", "write"): "file_write",
    ("mailbox", "read"): "mailbox_read",
    ("mailbox", "write"): "mailbox_write",
    ("sh", "kill"): "shell_kill",
    ("sh", "poll"): "shell_poll",
    ("sh", "run_async"): "shell_run_async",
    ("spi", "begin"): "spi_begin",
    ("spi", "config"): "spi_config",
    ("spi", "end"): "spi_end",
    ("spi", "transfer"): "spi_transfer",
    ("system", "bootloader"): "system_bootloader",
    ("system", "free_memory"): "system_free_memory",
    ("system", "version"): "system_version",
}


def allows_topic(auth: pb.TopicAuthorization, topic: str, action: str) -> bool:
    """Check if a specific topic/action combination is authorized. [SIL-2]"""
    field_name = _TOPIC_AUTH_MAP.get((topic.lower(), action.lower()))
    if field_name is not None:
        return bool(getattr(auth, field_name))
    return False


# =============================================================================
# 3. Runtime Configuration Structures (Protobuf-backed)
# =============================================================================


# Type alias — RuntimeConfig IS pb.RuntimeConfig, no proxy layer needed.
RuntimeConfig = pb.RuntimeConfig


def _format_violation(violation: ProtovalidateViolation) -> str:
    """Render a protovalidate Violation as 'field: message' (or bare message for message-level CEL rules)."""
    field_elements = violation.field.elements
    if field_elements:
        field_name = ".".join(e.field_name for e in field_elements)
        return f"{field_name}: {violation.message}"
    return violation.message


def validate_config(cfg: pb.RuntimeConfig) -> None:
    """Validate and normalize a RuntimeConfig in-place. [SIL-2]"""
    # Declarative validation (min_len/pattern/gte/lte, cross-field CEL rules
    # e.g. watchdog_interval, FLASH PROTECTION paths, mTLS cert/key pairing)
    # MUST run before any normalization mutates cfg below.
    # protovalidate.ValidationError.violations lack field context in their
    # default str(); re-raise as ValueError with field-qualified detail so
    # callers/tests can match on the offending field name. exc.to_proto()
    # returns the public buf.validate.Violations protobuf message, avoiding
    # any dependency on protovalidate's internal module layout.
    try:
        protovalidate.validate(cfg)
    except protovalidate.ValidationError as exc:
        details = "; ".join(_format_violation(v) for v in exc.to_proto().violations)
        raise ValueError(details) from exc

    cfg.allowed_policy.CopyFrom(create_allowed_policy(cfg.allowed_commands))
    del cfg.allowed_commands[:]
    cfg.allowed_commands.extend(cfg.allowed_policy.entries)

    auth_fields = [f.name for f in cfg.topic_authorization.DESCRIPTOR.fields]
    if not any(getattr(cfg.topic_authorization, name) for name in auth_fields):
        for name in auth_fields:
            setattr(cfg.topic_authorization, name, True)


@functools.lru_cache(maxsize=4)
def _build_cached_ssl_context(
    cloud_cafile: str, cloud_certfile: str, cloud_keyfile: str, cloud_tls_insecure: bool
) -> ssl.SSLContext:
    if cloud_cafile:
        ca_path = Path(cloud_cafile)
        if not ca_path.exists():
            raise RuntimeError(f"Cloud TLS CA file missing: {cloud_cafile}")
        context = ssl.create_default_context(ssl.Purpose.SERVER_AUTH, cafile=str(ca_path))
    else:
        context = ssl.create_default_context(ssl.Purpose.SERVER_AUTH)

    context.minimum_version = CLOUD_TLS_MIN_VERSION

    if cloud_tls_insecure:
        context.check_hostname = False
        context.verify_mode = ssl.CERT_NONE

    if cloud_certfile:
        context.load_cert_chain(cloud_certfile, cloud_keyfile)

    return context


def get_ssl_context(cfg: pb.RuntimeConfig) -> Any | None:
    """Create an ssl.SSLContext based on cfg. [SIL-2]"""
    if not cfg.cloud_tls:
        return None

    try:
        return _build_cached_ssl_context(
            cfg.cloud_cafile, cfg.cloud_certfile, cfg.cloud_keyfile, cfg.cloud_tls_insecure
        )
    except (OSError, ssl.SSLError, ValueError) as exc:
        raise RuntimeError(f"TLS setup failed: {exc}") from exc


# =============================================================================
# 3. Operational Structures
# =============================================================================


# --- Binary Protocol Packets ---


# --- High-Level Structure ---


@dataclass(slots=True)
class PendingPinRequest:
    pin: int
    reply_context: Any | None = None


# --- CLOUD Spool Helpers ---


UserProperty = tuple[str, str]


def replace_cloud_publish(message: pb.CloudQueuedPublish, **kwargs: Any) -> pb.CloudQueuedPublish:
    """Create a new CloudQueuedPublish with fields replaced."""
    newpb_obj = pb.CloudQueuedPublish()
    newpb_obj.CopyFrom(message)
    for k, v in kwargs.items():
        if k == "user_properties":
            del newpb_obj.user_properties[:]
            if v:
                newpb_obj.user_properties.extend(
                    [
                        item if isinstance(item, pb.UserProperty) else pb.UserProperty(key=item[0], value=item[1])
                        for item in v
                    ]
                )
        elif k == "subscription_identifier":
            del newpb_obj.subscription_identifier[:]
            if v is not None:
                newpb_obj.subscription_identifier.extend(v)
        else:
            setattr(newpb_obj, k, v)
    return newpb_obj


def resolve_cloud_context(message: pb.CloudQueuedPublish, context: Any | None) -> pb.CloudQueuedPublish:
    """Resolve CLOUD request-reply context into the publish message."""
    if context is None:
        return message

    newpb_obj = pb.CloudQueuedPublish()
    newpb_obj.CopyFrom(message)

    rt = getattr(context, "response_topic", None)
    if rt is None and (props := getattr(context, "properties", None)):
        rt = getattr(props, "ResponseTopic", None)
    if rt is not None:
        newpb_obj.topic_name = str(rt)

    cd = getattr(context, "correlation_data", None)
    if cd is None and (props := getattr(context, "properties", None)):
        cd = getattr(props, "CorrelationData", None)
    if cd is not None:
        newpb_obj.correlation_data = bytes(cd)

    if req_topic := getattr(context, "topic", None):
        newpb_obj.user_properties.add(key="bridge-request-topic", value=str(req_topic))

    return newpb_obj


def create_queued_publish(
    topic_name: str,
    payload: bytes,
    content_type: str | None = None,
    message_expiry_interval: int | None = None,
    user_properties: Iterable[tuple[str, str]] = (),
    qos: int = 1,
) -> pb.CloudQueuedPublish:
    """Factory to create a CloudQueuedPublish message. [SIL-2]"""
    msg = pb.CloudQueuedPublish(
        topic_name=topic_name,
        payload=payload,
        content_type=content_type or "",
        qos=qos,
    )
    if message_expiry_interval is not None:
        msg.message_expiry_interval = message_expiry_interval
    for k, v in user_properties:
        msg.user_properties.add(key=k, value=v)
    return msg


# --- Serial Flow Structures ---


@dataclass(slots=True)
class PendingCommand:
    """Book-keeping for a tracked command in flight. [SIL-2]"""

    command_id: int
    expected_resp_ids: list[int] = field(default_factory=lambda: [])
    reply_topic: str | None = None
    correlation_data: bytes | None = None
    attempts: int = 0
    success: bool | None = None
    failure_status: int | None = None
    ack_received: bool = False
    completion: asyncio.Event = field(default_factory=asyncio.Event)
    response_payload: bytes | ProtobufMessage | None = None

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
