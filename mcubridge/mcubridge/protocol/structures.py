"""MCU Bridge Data Structures and Schemas.

SINGLE SOURCE OF TRUTH for all data structures.
Binary parsing uses stdlib struct; high-level schemas use Protobuf (SIL-2) [TESTED].
"""

from __future__ import annotations

import asyncio
import fnmatch
import functools
import itertools
import re
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
from mcubridge.config.const import ALLOWED_COMMAND_WILDCARD


def iter_chunks(data: bytes, chunk_size: int) -> Iterable[bytes]:
    """Chunk bytes into fixed-size pieces. [SIL-2] Delegates to itertools.batched."""
    return (bytes(chunk) for chunk in batched(data, chunk_size))


PROTOBUF_CONTENT_TYPE: Final[str] = "application/x-protobuf"

# [SIL-2] Compiled once at module load; reused across all AllowedCommandPolicy instances.
_TOKEN_SEP: Final = re.compile(r"[,\s]+")


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
    all_tokens = list(
        itertools.chain.from_iterable(filter(None, _TOKEN_SEP.split(c.strip().lower())) for c in entries if c)
    )
    items: set[str] = set(all_tokens)
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
    from mcubridge.config.const import VOLATILE_STORAGE_PATHS

    # Declarative validation (min_len/pattern/gte/lte, e.g. topic_prefix and
    # watchdog_interval) MUST run before any normalization mutates cfg below.
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

    if not cfg.allow_non_tmp_paths:
        if not any(cfg.cloud_spool_dir.startswith(p) for p in VOLATILE_STORAGE_PATHS):
            msg = f"FLASH PROTECTION: cloud_spool_dir ({cfg.cloud_spool_dir}) must be in volatile storage"
            raise ValueError(msg)
        if not any(cfg.file_system_root.startswith(p) for p in VOLATILE_STORAGE_PATHS):
            msg = f"FLASH PROTECTION: file_system_root ({cfg.file_system_root}) must be in volatile storage"
            raise ValueError(msg)


def get_ssl_context(cfg: pb.RuntimeConfig) -> Any | None:
    """Create an ssl.SSLContext based on cfg. [SIL-2]"""
    if not cfg.cloud_tls:
        return None

    import ssl
    from mcubridge.config.const import CLOUD_TLS_MIN_VERSION

    try:
        if cfg.cloud_cafile:
            ca_path = Path(cfg.cloud_cafile)
            if not ca_path.exists():
                raise RuntimeError(f"Cloud TLS CA file missing: {cfg.cloud_cafile}")
            context = ssl.create_default_context(ssl.Purpose.SERVER_AUTH, cafile=str(ca_path))
        else:
            context = ssl.create_default_context(ssl.Purpose.SERVER_AUTH)

        context.minimum_version = CLOUD_TLS_MIN_VERSION

        if cfg.cloud_tls_insecure:
            context.check_hostname = False
            context.verify_mode = ssl.CERT_NONE

        if cfg.cloud_certfile or cfg.cloud_keyfile:
            if not (cfg.cloud_certfile and cfg.cloud_keyfile):
                raise ValueError("Both cloud_certfile and cloud_keyfile must be provided for mTLS.")
            context.load_cert_chain(cfg.cloud_certfile, cfg.cloud_keyfile)

        return context
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
            for pk, pv in v:
                newpb_obj.user_properties.add(key=pk, value=pv)
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

    updates: dict[str, Any] = {}

    rt = getattr(context, "response_topic", None)
    if rt is None:
        props = getattr(context, "properties", None)
        if props:
            rt = getattr(props, "ResponseTopic", None)
    if rt is not None:
        updates["topic_name"] = str(rt)

    cd = getattr(context, "correlation_data", None)
    if cd is None:
        props = getattr(context, "properties", None)
        if props:
            cd = getattr(props, "CorrelationData", None)
    if cd is not None:
        updates["correlation_data"] = bytes(cd)

    user_props = [(p.key, p.value) for p in message.user_properties]
    if req_topic := getattr(context, "topic", None):
        user_props.append(("bridge-request-topic", str(req_topic)))

    newpb_obj = pb.CloudQueuedPublish()
    newpb_obj.CopyFrom(message)
    if "topic_name" in updates:
        newpb_obj.topic_name = updates["topic_name"]
    if "correlation_data" in updates:
        newpb_obj.correlation_data = updates["correlation_data"]

    del newpb_obj.user_properties[:]
    for k, v in user_props:
        newpb_obj.user_properties.add(key=k, value=v)

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
