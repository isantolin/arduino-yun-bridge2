from __future__ import annotations

import structlog
from typing import TYPE_CHECKING, Any, cast
from pathlib import Path
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator, ValidationInfo

from ..config.common import (
    get_default_config,
    get_uci_config,
)
from mcubridge.protocol.structures import apply_derived_fields
from mcubridge.protocol import mcubridge_pb2 as pb
from mcubridge.config.const import DEFAULT_SERIAL_SHARED_SECRET, VOLATILE_STORAGE_PATHS

logger = structlog.get_logger(__name__)


class UciConfig(BaseModel):
    model_config = ConfigDict(extra="allow", coerce_numbers_to_str=False)

    topic_prefix: str = ""
    serial_response_timeout: float = 0.0
    serial_retry_timeout: float = 0.0
    watchdog_enabled: bool = False
    watchdog_interval: float = 0.0
    serial_shared_secret: bytes = b""
    file_storage_quota_bytes: int = 0
    file_write_max_bytes: int = 0
    mailbox_queue_bytes_limit: int = 0
    mailbox_queue_limit: int = 0
    status_interval: float | None = None
    serial_handshake_fatal_failures: int | None = None
    allow_non_tmp_paths: bool = False
    cloud_spool_dir: str = ""
    file_system_root: str = ""
    allowed_commands: list[str] = Field(default_factory=list)

    @field_validator("allowed_commands", mode="before")
    @classmethod
    def parse_allowed_commands(cls, v: Any) -> list[str]:
        if isinstance(v, str):
            return v.split()
        return v if v is not None else []

    @field_validator("*", mode="before")
    @classmethod
    def parse_paths_and_bools(cls, v: Any, info: ValidationInfo) -> Any:
        # Skip None: Pydantic v2 will raise for str fields if we return None explicitly.
        # Returning the sentinel PydanticUndefined is not public API, so we convert None
        # to "" for string coercion — the model fields all default to "".
        if v is None:
            # Return v unchanged; the model_validator(mode="before") already strips None
            # from string fields via the coerce_dynamic_auth step. If None still reaches
            # here for a required str field, let Pydantic surface the real error.
            return v
        field_name = info.field_name or ""
        is_path_field = any(
            x in field_name for x in ("_dir", "_file", "_root", "serial_port", "cloud_ca", "cloud_cert", "cloud_key")
        )
        if is_path_field and isinstance(v, str) and ("~" in v or "/" in v) and "\n" not in v:
            return str(Path(v).expanduser().resolve())

        # Pydantic v2 handles '1', 'true', 'yes', 'on' for bool natively!
        return v

    @model_validator(mode="before")
    @classmethod
    def coerce_dynamic_auth(cls, data: Any) -> Any:
        if not isinstance(data, dict):
            return data
        raw: dict[str, Any] = cast("dict[str, Any]", data)
        from mcubridge.protocol import mcubridge_pb2 as pb

        for auth_field in pb.TopicAuthorization.DESCRIPTOR.fields:
            for key_candidate in (f"cloud_allow_{auth_field.name}", f"allow_{auth_field.name}"):
                if key_candidate in raw:
                    val: Any = raw[key_candidate]
                    if isinstance(val, bool):
                        raw[auth_field.name] = val
                    else:
                        raw[auth_field.name] = str(val).lower() in ("1", "true", "yes", "on")
                    break

        for field in pb.RuntimeConfig.DESCRIPTOR.fields:
            if field.name not in raw or raw[field.name] is None:
                continue
            val: Any = raw[field.name]
            if field.type == field.TYPE_BOOL and isinstance(val, str):
                raw[field.name] = val.lower() in ("1", "true", "yes", "on")
            elif field.type == field.TYPE_BYTES and isinstance(val, str):
                raw[field.name] = val.strip().encode("utf-8")
            elif field.type == field.TYPE_STRING and isinstance(val, str):
                s = val.strip()
                raw[field.name] = s  # keep empty string; field default applies
        return raw

    @model_validator(mode="after")
    def validate_business_rules(self) -> "UciConfig":
        if not self.topic_prefix or not any(filter(None, self.topic_prefix.split("/"))):
            raise ValueError("topic_prefix must contain at least one segment")
        if self.serial_response_timeout < self.serial_retry_timeout * 2:
            raise ValueError("serial_response_timeout must be at least 2x serial_retry_timeout")
        if self.watchdog_enabled and self.watchdog_interval < 0.5:
            raise ValueError("watchdog_interval must be >= 0.5s when enabled")
        if not self.serial_shared_secret:
            raise ValueError("serial_shared_secret must be configured")
        if self.serial_shared_secret == b"changeme123":
            raise ValueError("serial_shared_secret placeholder is insecure")
        unique_symbols = {byte for byte in self.serial_shared_secret}
        if len(unique_symbols) < 4 and self.serial_shared_secret != DEFAULT_SERIAL_SHARED_SECRET:
            raise ValueError("serial_shared_secret must contain at least four distinct bytes")
        if self.status_interval is not None and self.status_interval <= 0:
            raise ValueError("status_interval must be positive")
        if self.serial_handshake_fatal_failures is not None and self.serial_handshake_fatal_failures <= 0:
            raise ValueError("serial_handshake_fatal_failures must be positive")
        if self.file_storage_quota_bytes < self.file_write_max_bytes:
            raise ValueError("file_storage_quota_bytes must be greater than or equal to file_write_max_bytes")
        if self.mailbox_queue_bytes_limit < self.mailbox_queue_limit:
            raise ValueError("mailbox_queue_bytes_limit must be greater than or equal to mailbox_queue_limit")
        if not self.allow_non_tmp_paths:
            if not any(self.cloud_spool_dir.startswith(p) for p in VOLATILE_STORAGE_PATHS):
                raise ValueError(
                    f"FLASH PROTECTION: cloud_spool_dir ({self.cloud_spool_dir}) must be in volatile storage"
                )
            if not any(self.file_system_root.startswith(p) for p in VOLATILE_STORAGE_PATHS):
                raise ValueError(
                    f"FLASH PROTECTION: file_system_root ({self.file_system_root}) must be in volatile storage"
                )
        return self


def _coerce_pb_value(field_type: int, val: Any) -> Any:
    """Coerce value to match exact Protobuf FieldDescriptor type."""
    from google.protobuf.descriptor import FieldDescriptor

    if val is None:
        return None
    if field_type in (
        FieldDescriptor.TYPE_UINT32,
        FieldDescriptor.TYPE_INT32,
        FieldDescriptor.TYPE_UINT64,
        FieldDescriptor.TYPE_INT64,
    ):
        try:
            return int(val)
        except (ValueError, TypeError):
            return 0
    if field_type in (FieldDescriptor.TYPE_FLOAT, FieldDescriptor.TYPE_DOUBLE):
        try:
            return float(val)
        except (ValueError, TypeError):
            return 0.0
    if field_type == FieldDescriptor.TYPE_BOOL:
        if isinstance(val, str):
            return val.lower() in ("1", "true", "yes", "on")
        return bool(val)
    if field_type == FieldDescriptor.TYPE_BYTES:
        if isinstance(val, str):
            return val.strip().encode("utf-8")
        return bytes(val)
    if field_type == FieldDescriptor.TYPE_STRING:
        return str(val)
    return val


def _runtime_config_factory(
    pb_msg: pb.RuntimeConfig | None = None,
    *,
    bypass_defaults: bool = False,
    **kwargs: Any,
) -> pb.RuntimeConfig:
    if pb_msg is not None:
        return pb_msg
    if not bypass_defaults:
        defaults = get_default_config()
        for k, v in defaults.items():
            if k not in kwargs:
                kwargs[k] = v
    if isinstance(kwargs.get("serial_shared_secret"), str):
        kwargs["serial_shared_secret"] = kwargs["serial_shared_secret"].encode("utf-8")

    validated = UciConfig(**kwargs)

    topic_auth = None
    if "topic_authorization" not in __import__("typing").cast("dict[str, Any]", getattr(validated, "model_extra", {})):
        # Create it from the top-level keys
        topic_auth = {}
        for auth_field in pb.TopicAuthorization.DESCRIPTOR.fields:
            if auth_field.name in __import__("typing").cast("dict[str, Any]", getattr(validated, "model_extra", {})):
                topic_auth[auth_field.name] = (
                    __import__("typing")
                    .cast("dict[str, Any]", getattr(validated, "model_extra", {}))
                    .pop(auth_field.name)
                )

    model_dict = __import__("typing").cast("dict[str, Any]", validated.model_dump(exclude_unset=True))
    for field in pb.RuntimeConfig.DESCRIPTOR.fields:
        if field.name in model_dict and model_dict[field.name] is not None:
            if hasattr(getattr(pb.RuntimeConfig(), field.name), "extend"):
                items: Any = model_dict[field.name]
                if isinstance(items, (list, tuple)):
                    raw_items: list[Any] = cast("list[Any]", items)
                    model_dict[field.name] = [_coerce_pb_value(field.type, i) for i in raw_items]
            else:
                model_dict[field.name] = _coerce_pb_value(field.type, model_dict[field.name])

    cfg = pb.RuntimeConfig(**model_dict)
    if topic_auth:
        for k, v in __import__("typing").cast("dict[str, Any]", topic_auth).items():
            setattr(
                cfg.topic_authorization, k, _coerce_pb_value(pb.TopicAuthorization.DESCRIPTOR.fields_by_name[k].type, v)
            )
    apply_derived_fields(cfg)
    return cfg


if TYPE_CHECKING:
    RuntimeConfig = pb.RuntimeConfig
else:
    RuntimeConfig = _runtime_config_factory


def _load_raw_config() -> tuple[dict[str, Any], str]:
    source = "defaults"
    config = get_default_config()
    try:
        uci_values = get_uci_config()
        if uci_values:
            config.update(uci_values)
            source = "uci"
    except (OSError, ValueError, RuntimeError, ImportError) as err:
        logger.warning("UCI configuration unavailable or locked (using safe defaults): %s", err)
    return config, source


_config_source: list[str] = ["uci"]


def get_config_source() -> str:
    return _config_source[0]


def load_runtime_config(overrides: dict[str, Any] | None = None) -> RuntimeConfig:
    raw_values, source = _load_raw_config()
    defaults = get_default_config()
    for k, v in defaults.items():
        if k not in raw_values:
            raw_values[k] = v
    if overrides:
        raw_values.update(overrides)
        source = "cli"
    _config_source[0] = source

    try:
        validated = UciConfig(**raw_values)
        model_dict = __import__("typing").cast("dict[str, Any]", validated.model_dump())

        # Build Protobuf message directly without wrappers
        msg = pb.RuntimeConfig()

        # Populate standard fields
        for field in msg.DESCRIPTOR.fields:
            if field.name in ("allowed_policy", "topic_authorization"):
                continue
            val = None
            if field.name in model_dict:
                val = model_dict[field.name]
            elif field.name in __import__("typing").cast("dict[str, Any]", getattr(validated, "model_extra", {})):
                val = __import__("typing").cast("dict[str, Any]", getattr(validated, "model_extra", {}))[field.name]

            if val is not None:
                if hasattr(getattr(msg, field.name), "extend"):
                    if isinstance(val, (list, tuple)):
                        raw_val: list[Any] = cast("list[Any]", val)
                        getattr(msg, field.name).extend([_coerce_pb_value(field.type, i) for i in raw_val])
                else:
                    setattr(msg, field.name, _coerce_pb_value(field.type, val))

        # Populate topic_authorization
        for auth_field in msg.topic_authorization.DESCRIPTOR.fields:
            if auth_field.name in __import__("typing").cast("dict[str, Any]", getattr(validated, "model_extra", {})):
                val = __import__("typing").cast("dict[str, Any]", getattr(validated, "model_extra", {}))[
                    auth_field.name
                ]
                if val is not None:
                    setattr(
                        msg.topic_authorization,
                        auth_field.name,
                        _coerce_pb_value(auth_field.type, val),
                    )

        apply_derived_fields(msg)
        return msg
    except (ValueError, TypeError) as e:
        if source == "uci":
            logger.critical("FATAL: UCI configuration is invalid: %s", e)
            raise RuntimeError(f"Invalid system configuration: {e}") from e
        logger.critical("Configuration validation failed: %s", e)
        raise
