"""Settings loader for the MCU Bridge daemon.

Configuration is loaded from OpenWrt UCI (package `mcubridge`, section
`general`) with sane defaults for non-OpenWrt environments.

Runtime configuration is intentionally **UCI-only**: environment variables are
not used.
"""

from __future__ import annotations
from google.protobuf import json_format
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


def _normalize_config_dict(raw: dict[str, Any]) -> tuple[dict[str, Any], bytes | None]:
    """Normalize raw dictionary keys and types for Protobuf ParseDict. [SIL-2]"""
    norm: dict[str, Any] = {}
    auth: dict[str, Any] = {}
    secret: bytes | None = None

    for k, v in raw.items():
        if k == "serial_shared_secret":
            if v is not None:
                secret = v if isinstance(v, bytes) else str(v).strip().encode("utf-8")
            continue

        if k == "allowed_commands":
            if isinstance(v, (list, tuple)):
                items = cast("list[Any] | tuple[Any, ...]", v)
                norm[k] = [str(x).strip() for x in items if str(x).strip()]
            elif isinstance(v, str):
                norm[k] = v.split()
            else:
                norm[k] = [] if v is None else [str(v)]
            continue

        field = pb.RuntimeConfig.DESCRIPTOR.fields_by_name.get(k)
        if field is not None:
            if field.type == FieldDescriptor.TYPE_BOOL:
                norm[k] = v if isinstance(v, bool) else str(v).lower() in ("1", "true", "yes", "on")
            elif any(x in k for x in ("_dir", "_file", "_root", "serial_port", "cloud_ca", "cloud_cert", "cloud_key")):
                s_val = str(v).strip()
                norm[k] = (
                    str(Path(s_val).expanduser().resolve())
                    if (("~" in s_val or "/" in s_val) and "\n" not in s_val)
                    else s_val
                )
            elif field.type == FieldDescriptor.TYPE_STRING:
                norm[k] = str(v).strip() if v is not None else ""
            else:
                norm[k] = v
            continue

        if k.startswith(("cloud_allow_", "allow_")):
            prefix = "cloud_allow_" if k.startswith("cloud_allow_") else "allow_"
            auth_key = k.removeprefix(prefix)
            auth_bool = v if isinstance(v, bool) else str(v).lower() in ("1", "true", "yes", "on")
            if auth_key in pb.TopicAuthorization.DESCRIPTOR.fields_by_name:
                auth[auth_key] = auth_bool
            else:
                for auth_field in pb.TopicAuthorization.DESCRIPTOR.fields:
                    if auth_field.name.startswith(f"{auth_key}_") or auth_field.name == auth_key:
                        auth[auth_field.name] = auth_bool
            continue

        norm[k] = v

    if auth:
        norm["topic_authorization"] = auth
    return norm, secret


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
    norm_dict, secret = _normalize_config_dict(raw_values)
    try:
        json_format.ParseDict(norm_dict, msg, ignore_unknown_fields=True)
        if secret is not None:
            msg.serial_shared_secret = secret
        validate_config(msg)
        return msg
    except (ValueError, TypeError, json_format.ParseError) as e:
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
    msg = pb.RuntimeConfig()
    defaults = get_default_config()
    for k, v in defaults.items():
        if isinstance(v, bytes):
            defaults[k] = v.decode("utf-8")
    json_format.ParseDict(defaults, msg, ignore_unknown_fields=True)

    if isinstance(data, (str, bytes)):
        json_format.Parse(data, msg, ignore_unknown_fields=True)
    else:
        norm_data, secret = _normalize_config_dict(data)
        json_format.ParseDict(norm_data, msg, ignore_unknown_fields=True)
        if secret is not None:
            msg.serial_shared_secret = secret

    if overrides:
        for k, v in overrides.items():
            if hasattr(msg, k):
                setattr(msg, k, v)

    validate_config(msg)
    _config_source[0] = "json"
    return msg
