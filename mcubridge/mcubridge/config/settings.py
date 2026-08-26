"""Settings loader for the MCU Bridge daemon.

Configuration is loaded from OpenWrt UCI (package `mcubridge`, section
`general`) with sane defaults for non-OpenWrt environments.

Runtime configuration is intentionally **UCI-only**: environment variables are
not used.
"""

from __future__ import annotations
from google.protobuf.descriptor import FieldDescriptor

import structlog
from typing import TYPE_CHECKING, Any, cast
from pathlib import Path


from ..config.common import (
    get_default_config,
    get_uci_config,
)
from mcubridge.protocol.structures import validate_config
from mcubridge.protocol import mcubridge_pb2 as pb

logger = structlog.get_logger(__name__)


def _runtime_config_factory(
    pb_msg: pb.RuntimeConfig | None = None,
    *,
    bypass_defaults: bool = False,
    **kwargs: Any,
) -> pb.RuntimeConfig:
    """Factory: create a validated pb.RuntimeConfig from kwargs or a pre-built message."""
    if pb_msg is not None:
        return pb_msg
    if not bypass_defaults:
        defaults = get_default_config()
        for k, v in defaults.items():
            if k not in kwargs:
                kwargs[k] = v
    if isinstance(kwargs.get("serial_shared_secret"), str):
        kwargs["serial_shared_secret"] = kwargs["serial_shared_secret"].encode("utf-8")
    cfg = pb.RuntimeConfig(**kwargs)
    validate_config(cfg)
    return cfg


# [SIL-2] RuntimeConfig is pb.RuntimeConfig at type-check time (zero wrapper).
# At runtime, this callable factory applies defaults + validate_config(),
# preserving backward compatibility with direct construction from kwargs.
if TYPE_CHECKING:
    RuntimeConfig = pb.RuntimeConfig
else:
    RuntimeConfig = _runtime_config_factory


def _load_raw_config() -> tuple[dict[str, Any], str]:
    """Load configuration from defaults and UCI (SIL 2).

    Precedence (highest first): UCI -> Defaults.
    Fallback: Defaults are used if UCI is missing, locked, or corrupt.
    """
    source = "defaults"
    config = get_default_config()

    try:
        # [SIL-2] Resilient load: fail-safe to defaults on any system error
        uci_values = get_uci_config()
        if uci_values:
            config.update(uci_values)
            source = "uci"
    except (OSError, ValueError, RuntimeError, ImportError) as err:
        # [SIL-2] UCI is optional for system survival. Log error and continue with defaults.
        logger.warning("UCI configuration unavailable or locked (using safe defaults): %s", err)

    return config, source


# [SIL-2] Module-level config source for observability — mutable list avoids `global`
_config_source: list[str] = ["uci"]


def get_config_source() -> str:
    """Return the source of the last loaded configuration ('uci' or 'defaults')."""
    return _config_source[0]


def _coerce_string(val: Any, field_name: str) -> str | None:
    s_val = str(val).strip()
    is_path_field = any(
        x in field_name for x in ("_dir", "_file", "_root", "serial_port", "cloud_ca", "cloud_cert", "cloud_key")
    )
    if is_path_field and ("~" in s_val or "/" in s_val) and "\n" not in s_val:
        return str(Path(s_val).expanduser().resolve())
    return s_val or None


def _coerce_int(val: Any, _field_name: str) -> int:
    try:
        return int(val)
    except (ValueError, TypeError):
        return 0


def _coerce_float(val: Any, _field_name: str) -> float:
    try:
        return float(val)
    except (ValueError, TypeError):
        return 0.0


def _coerce_bool(val: Any, _field_name: str) -> bool:
    return val if isinstance(val, bool) else str(val).lower() in ("1", "true", "yes", "on")


def _coerce_bytes(val: Any, _field_name: str) -> bytes:
    return val if isinstance(val, bytes) else str(val).strip().encode("utf-8")


_COERCERS: dict[int, Any] = {
    FieldDescriptor.TYPE_BYTES: _coerce_bytes,
    FieldDescriptor.TYPE_STRING: _coerce_string,
    FieldDescriptor.TYPE_INT32: _coerce_int,
    FieldDescriptor.TYPE_INT64: _coerce_int,
    FieldDescriptor.TYPE_UINT32: _coerce_int,
    FieldDescriptor.TYPE_UINT64: _coerce_int,
    FieldDescriptor.TYPE_FLOAT: _coerce_float,
    FieldDescriptor.TYPE_DOUBLE: _coerce_float,
    FieldDescriptor.TYPE_BOOL: _coerce_bool,
}

_CPPTYPE_COERCERS: dict[int, Any] = {
    FieldDescriptor.CPPTYPE_STRING: _coerce_string,
    FieldDescriptor.CPPTYPE_INT32: _coerce_int,
    FieldDescriptor.CPPTYPE_INT64: _coerce_int,
    FieldDescriptor.CPPTYPE_UINT32: _coerce_int,
    FieldDescriptor.CPPTYPE_UINT64: _coerce_int,
    FieldDescriptor.CPPTYPE_FLOAT: _coerce_float,
    FieldDescriptor.CPPTYPE_DOUBLE: _coerce_float,
    FieldDescriptor.CPPTYPE_BOOL: _coerce_bool,
}


def _coerce_value(val: Any, field: Any, field_name: str = "") -> Any:
    """Coerce UCI string values to target Protobuf types using FieldDescriptor metadata. [SIL-2]"""
    if val is None:
        return None
    target_type = getattr(field, "type", field)
    if coercer := _COERCERS.get(target_type):
        return coercer(val, field_name)
    cpp_type = getattr(field, "cpp_type", None)
    if cpp_type is not None and (cpp_coercer := _CPPTYPE_COERCERS.get(cpp_type)):
        return cpp_coercer(val, field_name)
    return val


def load_runtime_config(overrides: dict[str, Any] | None = None) -> RuntimeConfig:
    """Load, normalize, and validate the daemon configuration (SIL 2)."""
    raw_values, source = _load_raw_config()
    from mcubridge.config.common import get_default_config

    defaults = get_default_config()
    for k, v in defaults.items():
        if k not in raw_values:
            raw_values[k] = v
    if overrides:
        raw_values.update(overrides)
        source = "cli"
    _config_source[0] = source

    msg = pb.RuntimeConfig()

    if isinstance(raw_values.get("allowed_commands"), str):
        raw_values["allowed_commands"] = raw_values["allowed_commands"].split()
    elif raw_values.get("allowed_commands") is None:
        raw_values["allowed_commands"] = []

    for field in msg.DESCRIPTOR.fields:
        if field.name in ("allowed_policy", "topic_authorization"):
            continue

        val = raw_values.get(field.name)
        if val is None:
            continue

        if hasattr(getattr(msg, field.name), "extend"):
            if isinstance(val, (list, tuple)):
                items = [_coerce_value(i, field, field.name) for i in cast("list[Any]", val)]
                getattr(msg, field.name).extend(items)
            continue

        coerced = _coerce_value(val, field, field.name)
        if coerced is not None:
            setattr(msg, field.name, coerced)

    # Load topic authorizations dynamically from raw_values/UCI
    for auth_field in msg.topic_authorization.DESCRIPTOR.fields:
        for key_candidate in (
            f"cloud_allow_{auth_field.name}",
            f"allow_{auth_field.name}",
        ):
            if key_candidate in raw_values:
                coerced = _coerce_value(raw_values[key_candidate], auth_field, auth_field.name)
                if coerced is not None:
                    setattr(msg.topic_authorization, auth_field.name, coerced)
                break

    try:
        validate_config(msg)
        return msg
    except (ValueError, TypeError) as e:
        if source == "uci":
            logger.critical("FATAL: UCI configuration is invalid", error=str(e))
            raise RuntimeError(f"Invalid system configuration: {e}") from e
        logger.critical("Configuration validation failed", error=str(e))
        raise


def load_runtime_config_from_json(
    data: str | bytes | dict[str, Any],
    overrides: dict[str, Any] | None = None,
) -> RuntimeConfig:
    """Load, parse, and validate RuntimeConfig from JSON or Dict using native Protobuf json_format. [SIL-2]"""
    from google.protobuf import json_format

    msg = pb.RuntimeConfig()
    defaults = get_default_config()
    for k, v in defaults.items():
        if isinstance(v, bytes):
            defaults[k] = v.decode("utf-8", errors="replace")
    json_format.ParseDict(defaults, msg, ignore_unknown_fields=True)

    if isinstance(data, (str, bytes)):
        json_format.Parse(data, msg, ignore_unknown_fields=True)
    else:
        json_format.ParseDict(data, msg, ignore_unknown_fields=True)

    if overrides:
        for k, v in overrides.items():
            if hasattr(msg, k):
                setattr(msg, k, v)

    validate_config(msg)
    _config_source[0] = "json"
    return msg
