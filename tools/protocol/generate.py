#!/usr/bin/env python3
"""Protocol binding generator for MCU Bridge v2.

Architecture:
- Model: Strongly typed dataclasses representing the protocol spec.
- Jinja2: Declarative templates for C++ and Python outputs.

Copyright (C) 2025-2026 Ignacio Santolin and contributors
"""

from __future__ import annotations

import hashlib
import importlib
import importlib.util
import json
import re
import shutil
import subprocess
import sys
import tomllib
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Annotated, Any, TypeGuard, cast

import black
import httpx
import tenacity
import typer
from google.protobuf.descriptor import FieldDescriptor
from google.protobuf.json_format import MessageToDict
from jinja2 import Environment, FileSystemLoader
from packaging.version import Version

# ═════════════════════════════════════════════════════════════════════════════
# DEPENDENCY VALIDATION (CRITICAL)
# ═════════════════════════════════════════════════════════════════════════════
REQUIRED_DEPS = ["jinja2", "google.protobuf", "nanopb", "mypy_protobuf", "grpclib", "black"]

MISSING_DEPS: list[str] = [dep for dep in REQUIRED_DEPS if importlib.util.find_spec(dep.split(".")[0]) is None]

HAS_BUF: bool = shutil.which("buf") is not None

REPO_ROOT = Path(__file__).resolve().parent.parent.parent

if MISSING_DEPS or not HAS_BUF:
    sys.stderr.write("\n" + "!" * 80 + "\n")
    sys.stderr.write("ERROR: Missing dependencies required for protocol generation:\n")
    for dep in MISSING_DEPS:
        sys.stderr.write(f"  - {dep} (Python)\n")
    if not HAS_BUF:
        sys.stderr.write("  - buf (Buf CLI not found in PATH)\n")
    sys.stderr.write("\nTo fix this, run:\n")
    if MISSING_DEPS:
        sys.stderr.write(f"  pip install {' '.join(MISSING_DEPS)}\n")
    if not HAS_BUF:
        sys.stderr.write("  npm install -g @bufbuild/buf\n")
    sys.stderr.write("!" * 80 + "\n\n")
    sys.exit(1)
# ═════════════════════════════════════════════════════════════════════════════


# Mappings and helper functions for reflective protocol constant generation.


def _resolve_nanopb_protoc_dir() -> Path | None:
    """Locate the directory containing nanopb's bundled protoc binary."""
    spec = importlib.util.find_spec("nanopb")
    if spec and spec.origin:
        candidate = Path(spec.origin).parent / "generator"
        if (candidate / "protoc").exists():
            return candidate
    return None


def run_buf_format(proto_dir: Path) -> None:
    """Run ``buf format -w`` declaratively on proto specs."""
    try:
        subprocess.run(
            ["buf", "format", "-w"],
            check=True,
            capture_output=True,
            text=True,
            cwd=str(proto_dir),
        )
    except subprocess.CalledProcessError as e:
        raise RuntimeError(f"buf format failed:\n{e.stderr}") from e


def run_buf_generate(proto_dir: Path) -> None:
    """Run ``buf generate`` declaratively (replaces manual protoc/nanopb subprocess calls).

    Buf orchestrates all protoc plugins defined in ``buf.gen.yaml``:
    python pb2, mypy stubs, grpclib, and nanopb C.
    """
    import os

    env = os.environ.copy()
    py_bin_dir = str(Path(sys.executable).parent)
    env["PATH"] = f"{py_bin_dir}:{env.get('PATH', '')}"
    # Ensure nanopb's bundled protoc is reachable for buf's protoc_builtin plugins
    nanopb_dir = _resolve_nanopb_protoc_dir()
    if nanopb_dir:
        env["PATH"] = f"{nanopb_dir}:{env['PATH']}"

    try:
        subprocess.run(
            ["buf", "generate"],
            check=True,
            capture_output=True,
            text=True,
            cwd=str(proto_dir),
            env=env,
        )
    except subprocess.CalledProcessError as e:
        sys.stderr.write(f"Error: buf generate failed:\n{e.stderr}\n")
        sys.exit(1)


def _default_action_routes() -> list[dict[str, str]]:
    return []


@dataclass
class CommandDef:
    name: str
    value: int
    directions: list[str]
    category: str | None = None
    description: str | None = None
    requires_ack: bool = False
    expects_direct_response: bool = False
    pre_sync_allowed: bool = False
    cloud_topic: str | None = None
    pb_message: str | None = None
    response_command: str | None = None
    request_command: str | None = None
    auth_field: str | None = None
    rpc_method: str | None = None
    action_routes: list[dict[str, str]] = field(default_factory=_default_action_routes)
    mcu_handler: str | None = None
    mcu_unsupported_reason: str | None = None
    is_query: bool = False


@dataclass
class StatusDef:
    name: str
    value: int
    description: str
    mcu_handler: str | None = None


@dataclass
class ConfigFieldDef:
    name: str
    field_type: str
    default_value: Any
    raw_default: str
    description: str
    is_volatile: bool
    min_val: float | None
    max_val: float | None
    uci_option: str | None
    config_default_ref: str | None
    client_constant: bool


def _default_config_fields() -> list[ConfigFieldDef]:
    return []


@dataclass
class ProtocolSpec:
    constants: dict[str, Any]
    hardware: dict[str, Any]
    commands: list[CommandDef]
    statuses: list[StatusDef]
    handshake: dict[str, Any]
    cloud_subscriptions: list[dict[str, Any]]
    actions: list[dict[str, Any]]
    topics: list[dict[str, Any]]
    capabilities: dict[str, int]
    architectures: dict[str, int]
    cloud_suffixes: dict[str, str]
    cloud_defaults: dict[str, str]
    status_reasons: dict[str, str]
    architecture_display_names: dict[str, str]
    message_topics: dict[str, str]
    constants_opt: Any = None
    hardware_opt: Any = None
    handshake_opt: Any = None
    pb_module: Any = None
    runtime_config_fields: list[ConfigFieldDef] = field(default_factory=_default_config_fields)


def _proto_to_dict(msg: Any) -> dict[str, Any]:
    """Convert Protobuf message to dict with canonical preservation flags. [SIL-2]"""
    return MessageToDict(msg, preserving_proto_field_name=True, always_print_fields_with_no_presence=True)


_FIELD_CONVERTERS: dict[int, tuple[str, Callable[[Any], Any]]] = {
    FieldDescriptor.TYPE_STRING: ("str", lambda v: str(v) if v is not None else ""),
    FieldDescriptor.TYPE_BYTES: ("bytes", lambda v: v.encode("utf-8") if isinstance(v, str) else (v or b"")),
    FieldDescriptor.TYPE_BOOL: ("bool", lambda v: str(v).lower() in ("true", "1", "yes") if v is not None else False),
    FieldDescriptor.TYPE_FLOAT: ("float", lambda v: float(v) if v is not None else 0.0),
    FieldDescriptor.TYPE_DOUBLE: ("float", lambda v: float(v) if v is not None else 0.0),
    FieldDescriptor.TYPE_INT32: ("int", lambda v: int(v) if v is not None else 0),
    FieldDescriptor.TYPE_INT64: ("int", lambda v: int(v) if v is not None else 0),
    FieldDescriptor.TYPE_UINT32: ("int", lambda v: int(v) if v is not None else 0),
    FieldDescriptor.TYPE_UINT64: ("int", lambda v: int(v) if v is not None else 0),
}


def _load_runtime_config_fields(file_desc: Any, pb_module: Any) -> list[ConfigFieldDef]:
    runtime_config_desc = file_desc.message_types_by_name.get("RuntimeConfig")
    if not runtime_config_desc:
        return []

    file_options = file_desc.GetOptions()
    default_sources = {
        "constants": file_options.Extensions[pb_module.constants],
        "cloud_defaults": file_options.Extensions[pb_module.cloud_defaults],
    }
    runtime_config_fields: list[ConfigFieldDef] = []
    for field_desc in runtime_config_desc.fields:
        opts = field_desc.GetOptions()
        has_literal_default = opts.HasExtension(pb_module.config_default)
        has_default_ref = opts.HasExtension(pb_module.config_default_ref)
        config_default_ref: str | None = None
        if has_literal_default and has_default_ref:
            raise ValueError(f"RuntimeConfig field '{field_desc.name}' has both default value and default reference")
        if has_default_ref:
            reference = opts.Extensions[pb_module.config_default_ref]
            config_default_ref = reference
            source_name, separator, field_name = reference.partition(".")
            source = default_sources.get(source_name)
            if not separator or not field_name or source is None:
                raise ValueError(f"Invalid RuntimeConfig default reference '{reference}'")
            if field_name not in source.DESCRIPTOR.fields_by_name:
                raise ValueError(f"Unknown RuntimeConfig default reference '{reference}'")
            cfg_default = getattr(source, field_name)
        else:
            cfg_default = opts.Extensions[pb_module.config_default] if has_literal_default else None
        cfg_desc = opts.Extensions[pb_module.config_desc] if opts.HasExtension(pb_module.config_desc) else ""
        cfg_volatile = (
            opts.Extensions[pb_module.config_volatile] if opts.HasExtension(pb_module.config_volatile) else False
        )
        cfg_min = opts.Extensions[pb_module.config_min] if opts.HasExtension(pb_module.config_min) else None
        cfg_max = opts.Extensions[pb_module.config_max] if opts.HasExtension(pb_module.config_max) else None
        uci_opt = opts.Extensions[pb_module.uci_option] if opts.HasExtension(pb_module.uci_option) else None
        client_constant = (
            opts.Extensions[pb_module.client_constant] if opts.HasExtension(pb_module.client_constant) else False
        )

        if field_desc.is_repeated:
            py_type, typed_val = "list", []
        elif field_desc.type in _FIELD_CONVERTERS:
            py_type, converter = _FIELD_CONVERTERS[field_desc.type]
            typed_val = converter(cfg_default)
        else:
            py_type, typed_val = "message", None

        runtime_config_fields.append(
            ConfigFieldDef(
                name=field_desc.name,
                field_type=py_type,
                default_value=typed_val,
                raw_default=str(cfg_default) if cfg_default is not None else "",
                description=cfg_desc,
                is_volatile=cfg_volatile,
                min_val=cfg_min,
                max_val=cfg_max,
                uci_option=uci_opt,
                config_default_ref=config_default_ref,
                client_constant=client_constant,
            )
        )
    return runtime_config_fields


def load_spec_from_proto(proto_path: Path, pb_module: Any = None) -> ProtocolSpec:
    if pb_module is not None:
        mcubridge_pb2 = pb_module
    else:
        proto_dir = str(proto_path.parent)
        if proto_dir not in sys.path:
            sys.path.insert(0, proto_dir)
        mcubridge_pb2 = importlib.import_module("mcubridge_pb2")
    file_desc = mcubridge_pb2.DESCRIPTOR
    options = file_desc.GetOptions()

    constants_opt = options.Extensions[mcubridge_pb2.constants]
    hardware_opt = options.Extensions[mcubridge_pb2.hardware]
    handshake_opt = options.Extensions[mcubridge_pb2.handshake]
    cloud_suffixes_opt = options.Extensions[mcubridge_pb2.cloud_suffixes]
    cloud_defaults_opt = options.Extensions[mcubridge_pb2.cloud_defaults]
    status_reasons_opt = options.Extensions[mcubridge_pb2.status_reasons]
    cloud_subscriptions_opt = options.Extensions[mcubridge_pb2.cloud_subscriptions]
    topics_opt = options.Extensions[mcubridge_pb2.topics]
    actions_opt = options.Extensions[mcubridge_pb2.actions]
    architectures_opt = options.Extensions[mcubridge_pb2.architectures]
    capabilities_opt = options.Extensions[mcubridge_pb2.capabilities]

    constants = _proto_to_dict(constants_opt)
    hardware = _proto_to_dict(hardware_opt)
    handshake = _proto_to_dict(handshake_opt)
    cloud_suffixes = _proto_to_dict(cloud_suffixes_opt)
    for field_desc in mcubridge_pb2.CloudSuffixes.DESCRIPTOR.fields:
        cloud_suffixes.setdefault(field_desc.name, field_desc.name)

    cloud_defaults = _proto_to_dict(cloud_defaults_opt)
    status_reasons = _proto_to_dict(status_reasons_opt)
    for field_desc in mcubridge_pb2.StatusReasons.DESCRIPTOR.fields:
        status_reasons.setdefault(field_desc.name, field_desc.name)

    cloud_subscriptions: list[dict[str, Any]] = []
    for sub in cloud_subscriptions_opt:
        sub_dict = _proto_to_dict(sub)
        sub_dict.setdefault("qos", 1)
        cloud_subscriptions.append(sub_dict)
    topics = [_proto_to_dict(t) for t in topics_opt]
    actions = [_proto_to_dict(a) for a in actions_opt]

    architectures = {arch.name: arch.value for arch in architectures_opt}
    architecture_display_names = {arch.name: arch.display_name for arch in architectures_opt if arch.display_name}

    capabilities = {cap.name: cap.value for cap in capabilities_opt}

    # Load Command enum
    command_enum_desc = file_desc.enum_types_by_name["Command"]
    commands: list[CommandDef] = []
    for val in command_enum_desc.values:
        if val.name == "CMD_UNSPECIFIED":
            continue
        opts = val.GetOptions().Extensions[mcubridge_pb2.cmd_opts]
        action_routes = [
            {
                "topic": r.topic,
                "scope": r.scope,
                "action_enum": r.action_enum,
                "action": r.action,
                "handler": r.handler,
            }
            for r in getattr(opts, "action_routes", [])
        ]
        commands.append(
            CommandDef(
                name=val.name,
                value=val.number,
                directions=list(opts.directions),
                category=opts.category or None,
                description=opts.description or None,
                requires_ack=opts.requires_ack,
                expects_direct_response=opts.expects_direct_response,
                pre_sync_allowed=opts.pre_sync_allowed,
                cloud_topic=opts.cloud_topic or None,
                pb_message=opts.pb_message or None,
                response_command=opts.response_command or None,
                request_command=opts.request_command or None,
                auth_field=opts.auth_field or None,
                rpc_method=opts.rpc_method or None,
                action_routes=action_routes,
                mcu_handler=opts.mcu_handler or None,
                mcu_unsupported_reason=opts.mcu_unsupported_reason or None,
                is_query=opts.is_query,
            )
        )

    # Load Message & Enum CLOUD topics
    message_topics = {
        msg_name: opts.Extensions[mcubridge_pb2.msg_cloud_topic]
        for msg_name, msg_desc in file_desc.message_types_by_name.items()
        if (opts := msg_desc.GetOptions()).HasExtension(mcubridge_pb2.msg_cloud_topic)
    }
    message_topics.update(
        {
            f"{enum_name}_ENUM": opts.Extensions[mcubridge_pb2.enum_cloud_topic]
            for enum_name, enum_desc in file_desc.enum_types_by_name.items()
            if (opts := enum_desc.GetOptions()).HasExtension(mcubridge_pb2.enum_cloud_topic)
        }
    )

    # Load Status enum
    status_enum_desc = file_desc.enum_types_by_name["Status"]
    statuses = [
        StatusDef(
            name=val.name,
            value=val.number,
            description=(s_opts := val.GetOptions().Extensions[mcubridge_pb2.status_opts]).description,
            mcu_handler=s_opts.mcu_handler or None,
        )
        for val in status_enum_desc.values
        if val.name != "STATUS_UNSPECIFIED"
    ]

    runtime_config_fields = _load_runtime_config_fields(file_desc, mcubridge_pb2)

    spec = ProtocolSpec(
        constants=constants,
        hardware=hardware,
        commands=commands,
        statuses=statuses,
        handshake=handshake,
        cloud_subscriptions=cloud_subscriptions,
        actions=actions,
        topics=topics,
        capabilities=capabilities,
        architectures=architectures,
        cloud_suffixes=cloud_suffixes,
        cloud_defaults=cloud_defaults,
        status_reasons=status_reasons,
        architecture_display_names=architecture_display_names,
        message_topics=message_topics,
        runtime_config_fields=runtime_config_fields,
    )
    spec.constants_opt = constants_opt
    spec.hardware_opt = hardware_opt
    spec.handshake_opt = handshake_opt
    spec.pb_module = mcubridge_pb2
    return spec


TEMPLATE_DIR = Path(__file__).parent / "templates"
VERSION_PATH = REPO_ROOT / "VERSION"


def _build_constant_context(spec: ProtocolSpec, version: str) -> dict[str, Any]:
    parsed_version = Version(version)
    v_major, v_minor, v_patch = (
        parsed_version.major,
        parsed_version.minor,
        parsed_version.micro,
    )
    cpp_constants: list[dict[str, Any]] = []
    python_constants: list[dict[str, Any]] = []
    client_constants: list[dict[str, Any]] = []
    pb_module = spec.pb_module

    for pb_obj in (spec.constants_opt, spec.hardware_opt):
        for proto_field in pb_obj.DESCRIPTOR.fields:
            opts = proto_field.GetOptions()
            val = getattr(pb_obj, proto_field.name)
            cpp_name = opts.Extensions[pb_module.cpp_name]
            if cpp_name:
                cpp_constants.append(
                    {
                        "name": cpp_name,
                        "type": opts.Extensions[pb_module.cpp_type],
                        "value": val,
                    }
                )

            py_name = opts.Extensions[pb_module.py_name]
            py_type = opts.Extensions[pb_module.py_type]
            if py_name:
                if py_name == "FRAME_DELIMITER":
                    formatted_val: Any = f"bytes([ {val} ])"
                elif py_type == "bytes":
                    formatted_val = f'b"{val}"'
                elif py_type == "str":
                    formatted_val = f'"{val}"'
                else:
                    formatted_val = val
                constant = {"name": py_name, "type": py_type, "value": formatted_val}
                python_constants.append(constant)
                if pb_obj is spec.constants_opt and opts.Extensions[pb_module.client_constant]:
                    client_constants.append(constant)
    cpp_constants.extend(
        [
            {"name": "FIRMWARE_VERSION_MAJOR", "type": "uint8_t", "value": v_major},
            {"name": "FIRMWARE_VERSION_MINOR", "type": "uint8_t", "value": v_minor},
            {"name": "FIRMWARE_VERSION_PATCH", "type": "uint8_t", "value": v_patch},
        ]
    )
    python_constants.extend(
        {"name": f"CLOUD_SUFFIX_{key.upper()}", "type": "str", "value": f'"{value}"'}
        for key, value in spec.cloud_suffixes.items()
    )
    return {
        "constants": cpp_constants,
        "python_constants": python_constants,
        "client_constants": client_constants,
        "v_major": v_major,
        "v_minor": v_minor,
        "v_patch": v_patch,
    }


def _build_runtime_config_constants(spec: ProtocolSpec, python_constants: list[dict[str, Any]]) -> list[dict[str, Any]]:
    existing_constant_names = {constant["name"] for constant in python_constants}
    runtime_config_constants: list[dict[str, Any]] = []
    for config_field in spec.runtime_config_fields:
        if (
            config_field.default_value is None
            or config_field.field_type == "list"
            or config_field.config_default_ref is not None
        ):
            continue
        const_name = f"DEFAULT_{config_field.name.upper()}"
        if const_name in existing_constant_names:
            continue
        if config_field.field_type == "bytes":
            value = f'b"{config_field.raw_default}"'
        elif config_field.field_type == "str":
            value = f'"{config_field.default_value}"'
        else:
            value = str(config_field.default_value)
        runtime_config_constants.append({"name": const_name, "type": config_field.field_type, "value": value})
    return runtime_config_constants


def _build_client_runtime_config_constants(spec: ProtocolSpec) -> list[dict[str, Any]]:
    client_constants: list[dict[str, Any]] = []
    for config_field in spec.runtime_config_fields:
        if not config_field.client_constant:
            continue
        if config_field.default_value is None:
            raise ValueError(f"Client constant '{config_field.name}' must have a default value")
        if config_field.field_type not in {"str", "bool", "int", "float"}:
            raise ValueError(f"Unsupported client constant type '{config_field.field_type}'")
        client_constants.append(
            {
                "name": f"DEFAULT_{config_field.name.upper()}",
                "type": config_field.field_type,
                "value": config_field.default_value,
            }
        )
    return client_constants


def _build_handshake_context(spec: ProtocolSpec, pb_module: Any) -> dict[str, Any]:
    handshake_constants: list[dict[str, Any]] = []
    python_handshake_constants: list[dict[str, Any]] = []
    for proto_field in spec.handshake_opt.DESCRIPTOR.fields:
        opts = proto_field.GetOptions()
        name = opts.Extensions[pb_module.cpp_name]
        if name:
            handshake_constants.append(
                {
                    "name": name,
                    "type": opts.Extensions[pb_module.cpp_type],
                    "value": getattr(spec.handshake_opt, proto_field.name),
                }
            )
        py_name = opts.Extensions[pb_module.py_name]
        if py_name:
            value = getattr(spec.handshake_opt, proto_field.name)
            py_type = opts.Extensions[pb_module.py_type]
            if py_name == "FRAME_DELIMITER":
                formatted_value: Any = f"bytes([ {value} ])"
            elif py_type == "bytes":
                formatted_value: Any = f'b"{value}"'
            elif py_type == "str":
                formatted_value = f'"{value}"'
            else:
                formatted_value = value
            python_handshake_constants.append({"name": py_name, "type": py_type, "value": formatted_value})
    handshake = {
        "hkdf_salt": spec.handshake["hkdf_salt"],
        "hkdf_salt_bytes": ", ".join(f"0x{ord(char):02X}" for char in spec.handshake["hkdf_salt"]),
        "hkdf_salt_len": len(spec.handshake["hkdf_salt"]),
        "hkdf_info_auth": spec.handshake["hkdf_info_auth"],
        "hkdf_info_auth_bytes": ", ".join(f"0x{ord(char):02X}" for char in spec.handshake["hkdf_info_auth"]),
        "hkdf_info_auth_len": len(spec.handshake["hkdf_info_auth"]),
        "hkdf_info_session": spec.handshake["hkdf_info_session"],
        "hkdf_info_session_bytes": ", ".join(f"0x{ord(char):02X}" for char in spec.handshake["hkdf_info_session"]),
        "hkdf_info_session_len": len(spec.handshake["hkdf_info_session"]),
    }
    return {
        "handshake_constants": handshake_constants,
        "python_handshake_constants": python_handshake_constants,
        "handshake": handshake,
    }


def _build_action_context(spec: ProtocolSpec) -> dict[str, Any]:
    grouped_action_items: dict[str, list[dict[str, Any]]] = {}
    for action in spec.actions:
        if "_" in action["name"]:
            prefix, suffix = action["name"].split("_", 1)
            grouped_action_items.setdefault(prefix, []).append(
                {
                    "name": suffix,
                    "value": action["value"],
                    "description": action["description"],
                }
            )
    grouped_actions = [
        {
            "class_name": "DatastoreAction" if prefix == "DATASTORE" else f"{prefix.lower().title()}Action",
            "action_items": items,
        }
        for prefix, items in grouped_action_items.items()
    ]

    valid_topic_names = {topic["name"] for topic in spec.topics}
    subscriptions: list[dict[str, Any]] = []
    for subscription in spec.cloud_subscriptions:
        segments: list[str] = []
        topic_name = subscription["topic"]
        for segment in subscription.get("segments", []):
            if segment == "+":
                segments.append("CLOUD_WILDCARD_SINGLE")
            elif segment == "#":
                segments.append("CLOUD_WILDCARD_MULTI")
            else:
                matched_action = next(
                    (
                        action
                        for action in spec.actions
                        if topic_name in valid_topic_names
                        and action["name"].startswith(f"{topic_name}_")
                        and action["value"] == segment
                    ),
                    None,
                )
                if matched_action:
                    action_class = (
                        "DatastoreAction" if topic_name == "DATASTORE" else f"{topic_name.lower().title()}Action"
                    )
                    segments.append(f"{action_class}.{matched_action['name'].split('_', 1)[1]}.value")
                else:
                    segments.append(f'"{segment}"')
        subscriptions.append(
            {
                "topic": topic_name,
                "qos": subscription["qos"],
                "segments_tuple": f"({', '.join(segments)},)" if segments else "()",
            }
        )
    return {"grouped_actions": grouped_actions, "subscriptions": subscriptions}


def _build_rpc_context(pb_module: Any) -> tuple[list[dict[str, str]], list[dict[str, str]]]:
    file_desc = pb_module.DESCRIPTOR
    service_desc = file_desc.services_by_name.get("LocalBridge")
    if not service_desc:
        return [], []
    local_specs: list[dict[str, str]] = []
    gateway_specs: list[dict[str, str]] = []
    for method in service_desc.methods:
        if method.client_streaming or method.server_streaming:
            continue
        req_cls = method.input_type.name
        resp_cls = method.output_type.name
        resp_opts = method.output_type.GetOptions()
        error_init = (
            resp_opts.Extensions[pb_module.msg_error_init] if resp_opts.HasExtension(pb_module.msg_error_init) else ""
        )
        default_factory = f"lambda: pb.{resp_cls}({error_init})" if error_init else f"pb.{resp_cls}"
        spec = {
            "name": method.name,
            "req_cls": req_cls,
            "resp_cls": resp_cls,
            "default_factory": default_factory,
            "handler_name": f"execute_{JinjaGenerator.snake_case(method.name)}",
        }
        local_specs.append(spec)
        if method.input_type != method.output_type:
            gateway_specs.append(spec)
    return local_specs, gateway_specs


def _build_command_context(
    spec: ProtocolSpec,
    pb_module: Any,
    payload_fields_by_msg: dict[str, str],
    telemetry_topic_field_map: dict[str, str],
) -> dict[str, Any]:
    command_specs: list[dict[str, Any]] = []
    message_specs: dict[str, dict[str, Any]] = {}

    for cmd in spec.commands:
        pb_cls = cmd.pb_message if (cmd.pb_message and hasattr(pb_module, cmd.pb_message)) else None
        p_field = payload_fields_by_msg.get(cmd.pb_message) if cmd.pb_message else None
        resps = [cmd.response_command] if cmd.response_command else []
        command_specs.append(
            {
                "name": cmd.name,
                "value": cmd.value,
                "pb_class": pb_cls,
                "payload_field": p_field,
                "cloud_topic": cmd.cloud_topic,
                "expected_responses": resps,
                "paired_request_id": cmd.request_command,
                "auth_field": cmd.auth_field,
                "rpc_method": cmd.rpc_method,
                "requires_ack": cmd.requires_ack,
                "expects_direct_response": cmd.expects_direct_response,
                "pre_sync_allowed": cmd.pre_sync_allowed,
                "category": cmd.category,
                "description": cmd.description,
            }
        )
        if cmd.pb_message and cmd.pb_message not in message_specs:
            message_specs[cmd.pb_message] = {
                "is_command": True,
                "cmd_name": cmd.name,
            }

    # Add any remaining envelope payload messages
    for msg_name, field_name in payload_fields_by_msg.items():
        if msg_name not in message_specs:
            blob_field = next((bf for tm, bf in telemetry_topic_field_map.items() if tm in msg_name.lower()), None)
            message_specs[msg_name] = {
                "is_command": False,
                "name": msg_name,
                "pb_class": msg_name if hasattr(pb_module, msg_name) else None,
                "payload_field": field_name,
                "cloud_topic": spec.message_topics.get(msg_name),
                "auth_field": None,
                "metrics_blob_field": blob_field,
            }

    # Add autonomous telemetry topics
    for msg_name, topic in spec.message_topics.items():
        if msg_name.endswith("_ENUM"):
            enum_name = msg_name[:-5]
            if enum_name not in message_specs:
                message_specs[enum_name] = {
                    "is_command": False,
                    "name": enum_name,
                    "pb_class": None,
                    "payload_field": None,
                    "cloud_topic": topic,
                    "auth_field": None,
                    "metrics_blob_field": None,
                }
        elif msg_name not in message_specs:
            blob_field = next((bf for tm, bf in telemetry_topic_field_map.items() if tm in topic), None)
            if blob_field is None and "status" in topic:
                blob_field = "system_status_blob"
            message_specs[msg_name] = {
                "is_command": False,
                "name": msg_name,
                "pb_class": msg_name if hasattr(pb_module, msg_name) else None,
                "payload_field": payload_fields_by_msg.get(msg_name),
                "cloud_topic": topic,
                "auth_field": None,
                "metrics_blob_field": blob_field,
            }

    return {
        "command_specs": command_specs,
        "message_specs": message_specs,
        "ack_commands": [command for command in spec.commands if command.requires_ack],
        "pre_sync_allowed_commands": [command for command in spec.commands if command.pre_sync_allowed],
        "response_only_commands": [command for command in spec.commands if command.expects_direct_response],
    }


def _build_descriptor_context(spec: ProtocolSpec, pb_module: Any) -> dict[str, Any]:
    file_desc = pb_module.DESCRIPTOR
    all_message_names = list(file_desc.message_types_by_name)
    options_path = (REPO_ROOT / "tools" / "protocol" / "mcubridge.options").resolve()
    options_content = options_path.read_text(encoding="utf-8")
    skipped_messages: set[str] = set()
    for line in options_content.splitlines():
        trimmed = line.strip()
        if not trimmed or trimmed.startswith("#"):
            continue
        parts = trimmed.split()
        if len(parts) >= 2 and parts[0].startswith("rpc.pb.") and "skip_message:true" in parts[1:]:
            msg_name = parts[0].removeprefix("rpc.pb.")
            if msg_name.isidentifier():
                skipped_messages.add(msg_name)
    all_structs = [
        {"name": name} for name in all_message_names if name not in skipped_messages and name != "RpcContainer"
    ]
    envelope_desc = file_desc.message_types_by_name.get("RpcEnvelope")
    payload_fields: list[dict[str, str]] = []
    payload_structs: list[dict[str, str]] = []
    if envelope_desc and "payload_type" in envelope_desc.oneofs_by_name:
        for field_desc in envelope_desc.oneofs_by_name["payload_type"].fields:
            if field_desc.message_type:
                payload = {
                    "name": field_desc.message_type.name,
                    "field": field_desc.name,
                }
                payload_fields.append(payload)
                if field_desc.message_type.name not in skipped_messages:
                    payload_structs.append(payload)

    topic_auth_map: dict[tuple[str, str], str] = {}
    auth_message = getattr(pb_module, "TopicAuthorization", None)
    if auth_message:
        topic_prefixes = {"analog": "a", "digital": "d", "shell": "sh"}
        for auth_field in auth_message.DESCRIPTOR.fields:
            name = auth_field.name
            if name == "console_input":
                topic_auth_map[("console", "in")] = name
                continue
            for prefix, abbreviation in topic_prefixes.items():
                if name.startswith(f"{prefix}_"):
                    topic_auth_map[(abbreviation, name.removeprefix(f"{prefix}_"))] = name
                    break
            else:
                parts = name.split("_", 1)
                if len(parts) == 2:
                    topic_auth_map[(parts[0], parts[1])] = name
    return {
        "all_structs": all_structs,
        "payload_fields": payload_fields,
        "payload_structs": payload_structs,
        "payload_names": [payload["name"] for payload in payload_structs],
        "topic_auth_map": topic_auth_map,
    }


def _build_telemetry_field_context(pb_module: Any) -> tuple[dict[str, str], str]:
    telemetry_fields = pb_module.TelemetryReport.DESCRIPTOR.fields
    topic_match_extension = pb_module.telemetry_topic_match
    topic_default_extension = pb_module.telemetry_topic_default
    topic_field_map: dict[str, str] = {}
    default_field: str | None = None

    for field_desc in telemetry_fields:
        options = field_desc.GetOptions()
        has_topic_match = options.HasExtension(topic_match_extension)
        has_default = options.HasExtension(topic_default_extension)
        if has_topic_match and has_default:
            raise ValueError(f"Telemetry field '{field_desc.name}' cannot be both matched and default")

        if has_topic_match:
            topic_match = options.Extensions[topic_match_extension]
            if not topic_match:
                raise ValueError(f"Telemetry field '{field_desc.name}' has an empty topic match")
            if topic_match in topic_field_map:
                raise ValueError(f"Duplicate telemetry topic match '{topic_match}'")
            topic_field_map[topic_match] = field_desc.name
        elif has_default:
            if not options.Extensions[topic_default_extension]:
                raise ValueError(f"Telemetry default marker for '{field_desc.name}' must be true")
            if default_field is not None:
                raise ValueError(f"Multiple telemetry default fields: '{default_field}' and '{field_desc.name}'")
            default_field = field_desc.name
        else:
            raise ValueError(f"Telemetry field '{field_desc.name}' has no routing metadata")

    if default_field is None:
        raise ValueError("TelemetryReport must define exactly one default field")
    if not topic_field_map:
        raise ValueError("TelemetryReport must define at least one topic match")

    return topic_field_map, default_field


def _build_command_action_routes(spec: ProtocolSpec) -> list[dict[str, str]]:
    """Derive canonical action routes directly from Command action_routes in .proto (SSOT). [SIL-2]"""
    routes: list[dict[str, str]] = []
    for cmd in spec.commands:
        for r in cmd.action_routes:
            if not r.get("handler"):
                continue
            action_code = f"{r['action_enum']}.{r['action']}" if r.get("action") else "None"
            routes.append(
                {
                    "topic": f"Topic.{r['topic']}",
                    "scope": r["scope"],
                    "action_code": action_code,
                    "handler": r["handler"],
                }
            )
    return routes


def _build_topic_dispatch_handlers(spec: ProtocolSpec) -> list[dict[str, str]]:
    """Derive topic dispatch handlers directly from TopicConfig handler in .proto (SSOT). [SIL-2]"""
    return [
        {"topic": f"Topic.{topic['name']}", "handler": topic["handler"]}
        for topic in spec.topics
        if topic.get("handler")
    ]


def _build_mcu_dispatch_context(spec: ProtocolSpec) -> tuple[list[dict[str, str]], list[dict[str, str]]]:
    """Derive MCU frame dispatch handlers and unsupported reasons from .proto (SSOT). [SIL-2]"""
    handlers: list[dict[str, str]] = [
        {"code": f"Status.{s.name}.value", "handler": s.mcu_handler} for s in spec.statuses if s.mcu_handler
    ]
    handlers.extend(
        {"code": f"Command.{c.name}.value", "handler": c.mcu_handler} for c in spec.commands if c.mcu_handler
    )
    unsupported: list[dict[str, str]] = [
        {
            "code": f"Command.{c.name}.value",
            "reason": f"STATUS_REASON_{c.mcu_unsupported_reason.upper()}",
        }
        for c in spec.commands
        if c.mcu_unsupported_reason
    ]
    return handlers, unsupported


def _build_query_topic_actions(spec: ProtocolSpec) -> list[tuple[str, str]]:
    """Derive query (topic, action) pairs directly from is_query Command options in .proto (SSOT). [SIL-2]"""
    seen: set[tuple[str, str]] = set()
    query_actions: list[tuple[str, str]] = []
    for cmd in spec.commands:
        if not getattr(cmd, "is_query", False):
            continue
        for route in cmd.action_routes:
            if route.get("action") and route.get("action_enum"):
                entry = (f"Topic.{route['topic']}", f"{route['action_enum']}.{route['action']}")
                if entry not in seen:
                    seen.add(entry)
                    query_actions.append(entry)
    return query_actions


def build_protocol_context(spec: ProtocolSpec, version: str) -> dict[str, Any]:
    """Build all template data from the protocol model and its descriptors."""
    constant_context = _build_constant_context(spec, version)
    handshake_context = _build_handshake_context(spec, spec.pb_module)
    action_context = _build_action_context(spec)
    descriptor_context = _build_descriptor_context(spec, spec.pb_module)
    telemetry_topic_field_map, telemetry_default_field = _build_telemetry_field_context(spec.pb_module)
    payload_fields_by_msg = {p["name"]: p["field"] for p in descriptor_context["payload_fields"]}
    command_context = _build_command_context(spec, spec.pb_module, payload_fields_by_msg, telemetry_topic_field_map)
    local_rpc_specs, gateway_rpc_specs = _build_rpc_context(spec.pb_module)
    command_action_routes = _build_command_action_routes(spec)
    topic_dispatch_handlers = _build_topic_dispatch_handlers(spec)
    mcu_dispatch_handlers, mcu_unsupported_reasons = _build_mcu_dispatch_context(spec)
    query_topic_actions = _build_query_topic_actions(spec)
    return {
        **constant_context,
        **handshake_context,
        **action_context,
        **command_context,
        **descriptor_context,
        "local_rpc_specs": local_rpc_specs,
        "gateway_rpc_specs": gateway_rpc_specs,
        "query_topic_actions": query_topic_actions,
        "runtime_config_constants": _build_runtime_config_constants(spec, constant_context["python_constants"]),
        "client_runtime_config_constants": _build_client_runtime_config_constants(spec),
        "runtime_config_fields": spec.runtime_config_fields,
        "capabilities": spec.capabilities,
        "architectures": spec.architectures,
        "architecture_display_names": spec.architecture_display_names,
        "cloud_suffixes": spec.cloud_suffixes,
        "cloud_defaults": spec.cloud_defaults,
        "status_reasons": spec.status_reasons,
        "statuses": spec.statuses,
        "commands": spec.commands,
        "topics": spec.topics,
        "message_topics": spec.message_topics,
        "telemetry_topic_field_map": telemetry_topic_field_map,
        "telemetry_default_field": telemetry_default_field,
        "spi_bit_orders": [
            {"name": value.name, "value": value.number} for value in spec.pb_module.SpiBitOrder.DESCRIPTOR.values
        ],
        "spi_data_modes": [
            {"name": value.name, "value": value.number} for value in spec.pb_module.SpiDataMode.DESCRIPTOR.values
        ],
        "hardware": spec.hardware,
        "command_action_routes": command_action_routes,
        "topic_dispatch_handlers": topic_dispatch_handlers,
        "mcu_dispatch_handlers": mcu_dispatch_handlers,
        "mcu_unsupported_reasons": mcu_unsupported_reasons,
    }


class JinjaGenerator:
    def __init__(self) -> None:
        self.env = Environment(
            loader=FileSystemLoader(str(TEMPLATE_DIR)),
            keep_trailing_newline=True,
        )
        cast(dict[str, Any], self.env.filters)["cpp_digits"] = self._cpp_digit_separator
        cast(dict[str, Any], self.env.filters)["snakecase"] = self._snake_case
        cast(dict[str, Any], self.env.filters)["py_str"] = self._py_str

    @staticmethod
    def _split_py_string(s: str, width: int = 70) -> list[str]:
        words = s.split(" ")
        lines: list[str] = []
        current: list[str] = []
        current_len = 0
        for w in words:
            added_len = len(w) + (1 if current else 0)
            if current and current_len + added_len > width:
                lines.append(" ".join(current) + " ")
                current = [w]
                current_len = len(w)
            else:
                current.append(w)
                current_len += added_len
        if current:
            lines.append(" ".join(current))
        return lines

    @staticmethod
    def _py_str(value: object, indent: int = 8, max_width: int = 100) -> str:
        """Format a Python literal, splitting long strings into parenthesized chunks. [SIL-2]"""
        if value is None:
            return "None"
        if not isinstance(value, str):
            return json.dumps(value)
        encoded = json.dumps(value)
        if len(encoded) + indent <= max_width:
            return encoded
        target_width = max(20, max_width - indent - 8)
        parts = JinjaGenerator._split_py_string(value, width=target_width)
        if len(parts) <= 1:
            return encoded
        indent_str = " " * indent
        formatted_parts = [f"{indent_str}    {json.dumps(p)}" for p in parts]
        return "(\n" + "\n".join(formatted_parts) + f"\n{indent_str})"

    @staticmethod
    def _cpp_digit_separator(value: object) -> str:
        """Format integers >= 10'000 with C++14 digit separators. [SIL-2]"""
        if not isinstance(value, int) or abs(value) < 10_000:
            return str(value)
        formatted = f"{abs(value):_}".replace("_", "'")
        return f"-{formatted}" if value < 0 else formatted

    @staticmethod
    def snake_case(s: str) -> str:
        return re.sub(r"(?<!^)(?=[A-Z])", "_", s).lower()

    _snake_case = snake_case

    def render_template(
        self,
        template_name: str,
        context: dict[str, Any],
        out_path: Path,
        *,
        create_parent: bool = False,
        executable: bool = False,
        format_python: bool = False,
    ) -> None:
        if create_parent:
            out_path.parent.mkdir(parents=True, exist_ok=True)
        rendered = self.env.get_template(template_name).render(**context)
        if format_python or out_path.suffix in (".py", ".pyi"):
            rendered = black.format_str(rendered, mode=black.Mode())
        if not out_path.is_file() or out_path.read_text(encoding="utf-8") != rendered:
            out_path.write_text(rendered, encoding="utf-8")
        if executable:
            out_path.chmod(0o755)


def update_metadata(version: str) -> None:
    targets = [
        (REPO_ROOT / "pyproject.toml", r'version\s*=\s*"[^"]+"', f'version = "{version}"', 1),
        (
            REPO_ROOT / "mcubridge" / "mcubridge" / "__init__.py",
            r'__version__\s*=\s*"[^"]+"',
            f'__version__ = "{version}"',
            1,
        ),
        (REPO_ROOT / "mcubridge" / "Makefile", r"PKG_VERSION:=[^\n]+", f"PKG_VERSION:={version}", 0),
        (REPO_ROOT / "mcubridge-gateway" / "Makefile", r"PKG_VERSION:=[^\n]+", f"PKG_VERSION:={version}", 0),
        (REPO_ROOT / "luci-app-mcubridge" / "Makefile", r"PKG_VERSION:=[^\n]+", f"PKG_VERSION:={version}", 0),
        (REPO_ROOT / "mcubridge-library-arduino" / "library.properties", r"version=[^\n]+", f"version={version}", 0),
    ]
    for target_path, pattern, repl, count in targets:
        if target_path.exists():
            original = target_path.read_text(encoding="utf-8")
            updated = re.sub(pattern, repl, original, count=count)
            if updated != original:
                target_path.write_text(updated, encoding="utf-8")
                sys.stderr.write(f"Updated {target_path} to version {version}\n")


def ensure_nanopb_core_files() -> None:
    """Ensure the core Nanopb C files exist in mcubridge-library-arduino/src/."""

    src_dir = REPO_ROOT / "mcubridge-library-arduino" / "src"
    version, files = _load_nanopb_core_spec()
    base_url = f"https://raw.githubusercontent.com/nanopb/nanopb/nanopb-{version}/"

    src_dir.mkdir(parents=True, exist_ok=True)
    for file_name in files:
        target = src_dir / file_name
        if not target.exists():
            url = base_url + file_name
            sys.stderr.write(f"Downloading core Nanopb file: {file_name} from {url}...\n")
            retryer = tenacity.Retrying(
                stop=tenacity.stop_after_attempt(3),
                wait=tenacity.wait_exponential(multiplier=1.0, min=1.0, max=5.0),
                retry=tenacity.retry_if_exception_type((httpx.HTTPError, OSError, TimeoutError)),
                reraise=True,
            )

            def _fetch_nanopb_file(target_url: str = url) -> bytes:
                resp = httpx.get(target_url, timeout=20.0, follow_redirects=True)
                resp.raise_for_status()
                return resp.content

            try:
                content = retryer(_fetch_nanopb_file)
                target.write_bytes(content)
            except (
                httpx.HTTPError,
                OSError,
                ValueError,
                tenacity.RetryError,
            ) as e:
                sys.stderr.write(f"Error downloading {file_name} after retries: {e}\n")
                sys.exit(1)


def _load_nanopb_core_spec() -> tuple[str, list[str]]:
    manifest_path = REPO_ROOT / "requirements" / "runtime.toml"
    try:
        with manifest_path.open("rb") as manifest_file:
            manifest: object = tomllib.load(manifest_file)
    except (OSError, tomllib.TOMLDecodeError) as exc:
        raise RuntimeError(f"Cannot load dependency manifest {manifest_path}: {exc}") from exc

    if not _is_string_keyed_mapping(manifest):
        raise RuntimeError(f"Invalid dependency manifest structure: {manifest_path}")
    dependencies = manifest.get("cpp_dependency")
    if not isinstance(dependencies, list):
        raise RuntimeError(f"Dependency manifest has no C++ dependency list: {manifest_path}")

    matches = [
        candidate
        for candidate in cast(list[object], dependencies)
        if _is_string_keyed_mapping(candidate) and candidate.get("name") == "nanopb_core"
    ]
    if len(matches) != 1:
        raise RuntimeError(f"Expected one nanopb_core entry in {manifest_path}, found {len(matches)}")

    dependency = matches[0]
    version = dependency.get("version")
    raw_files = dependency.get("files")
    if not isinstance(version, str) or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.+-]*", version):
        raise RuntimeError(f"Invalid nanopb_core version in {manifest_path}")
    if not isinstance(raw_files, list) or not raw_files:
        raise RuntimeError(f"Invalid nanopb_core file list in {manifest_path}")
    files: list[str] = []
    for file_name in cast(list[object], raw_files):
        if not isinstance(file_name, str) or not re.fullmatch(r"[A-Za-z0-9_.-]+", file_name):
            raise RuntimeError(f"Invalid nanopb_core file list in {manifest_path}")
        files.append(file_name)
    if len(set(files)) != len(files):
        raise RuntimeError(f"Duplicate nanopb_core files in {manifest_path}")
    return version, files


def _is_string_keyed_mapping(value: object) -> TypeGuard[dict[str, object]]:
    if not isinstance(value, dict):
        return False
    mapping = cast(dict[object, object], value)
    return all(isinstance(key, str) for key in mapping)


def _resolve_proto_path(spec_path: Path) -> Path:
    resolved_spec = spec_path.resolve()
    return (resolved_spec.parent / "mcubridge.proto").resolve() if resolved_spec.suffix == ".toml" else resolved_spec


def _template_target_specs(args: Any, repo_root: Path) -> list[tuple[str, str, Path | None, bool, bool]]:
    return [
        ("rpc_protocol.h.j2", "common", args.cpp, False, False),
        (
            "rpc_hw_config.h.j2",
            "common",
            args.cpp.parent / "rpc_hw_config.h" if args.cpp else None,
            False,
            False,
        ),
        ("rpc_structs.h.j2", "common", args.cpp_structs, False, False),
        ("protocol.py.j2", "python", args.py, False, True),
        ("protocol_client.py.j2", "client", args.py_client, False, True),
        (
            "mcubridge_uci.j2",
            "common",
            repo_root / "luci-app-mcubridge" / "root" / "etc" / "config" / "mcubridge",
            False,
            False,
        ),
        ("defaults_sh.j2", "common", repo_root / "mcubridge" / "scripts" / "defaults.sh", True, False),
        (
            "config_schema_json.j2",
            "common",
            repo_root
            / "luci-app-mcubridge"
            / "htdocs"
            / "luci-static"
            / "resources"
            / "view"
            / "mcubridge"
            / "config_schema.json",
            False,
            False,
        ),
    ]


def expected_generation_outputs(args: Any, *, repo_root: Path = REPO_ROOT) -> list[Path]:
    explicit_outputs = [path for path in (args.cpp, args.cpp_structs, args.py, args.py_client) if path]
    outputs = list(explicit_outputs)
    if args.cpp:
        outputs.extend((args.cpp.parent / "mcubridge.pb.h", args.cpp.parent / "mcubridge.pb.c"))

    python_output_dirs = {path.parent for path in (args.py, args.py_client) if path}
    for output_dir in python_output_dirs:
        outputs.extend(
            output_dir / file_name for file_name in ("mcubridge_pb2.py", "mcubridge_pb2.pyi", "mcubridge_grpc.py")
        )

    explicit_output_dirs = {path.parent for path in explicit_outputs}
    outputs.extend(
        target
        for _, _, target, _, _ in _template_target_specs(args, repo_root)
        if target and (target.parent.exists() or target in explicit_outputs or target.parent in explicit_output_dirs)
    )
    return list(dict.fromkeys(outputs))


def _generation_input_paths(args: Any, proto_path: Path, repo_root: Path) -> list[Path]:
    templates_dir = repo_root / "tools" / "protocol" / "templates"
    template_paths = sorted(templates_dir.glob("*.j2"))
    if not template_paths:
        raise FileNotFoundError(f"No protocol templates found in {templates_dir}")

    input_paths = [
        args.spec.resolve(),
        proto_path,
        Path(__file__).resolve(),
        proto_path.parent / "buf.yaml",
        proto_path.parent / "buf.gen.yaml",
        proto_path.parent / "mcubridge.options",
        repo_root / "requirements" / "runtime.toml",
        repo_root / ".github" / "actions" / "setup-protoc" / "action.yml",
        *template_paths,
    ]
    version_path = repo_root / "VERSION"
    if version_path.is_file():
        input_paths.append(version_path)

    unique_paths = list(dict.fromkeys(path.resolve() for path in input_paths))
    missing = [path for path in unique_paths if not path.is_file()]
    if missing:
        missing_list = ", ".join(str(path) for path in missing)
        raise FileNotFoundError(f"Protocol generation inputs are missing: {missing_list}")
    return unique_paths


def check_incremental_build(args: Any, version: str, *, repo_root: Path = REPO_ROOT) -> tuple[bool, Path, str]:
    proto_path = _resolve_proto_path(args.spec)
    input_paths = _generation_input_paths(args, proto_path, repo_root)
    h = hashlib.sha256()
    for input_path in input_paths:
        try:
            input_name = input_path.relative_to(repo_root.resolve()).as_posix()
        except ValueError:
            input_name = input_path.as_posix()
        input_content = input_path.read_bytes()
        h.update(input_name.encode("utf-8"))
        h.update(len(input_content).to_bytes(8, "big"))
        h.update(input_content)
    buf_version = subprocess.run(["buf", "--version"], check=True, capture_output=True, text=True).stdout.strip()
    if not buf_version:
        raise RuntimeError("buf --version returned an empty version")
    h.update(b"buf-version\0")
    h.update(buf_version.encode("utf-8"))
    h.update(version.encode("utf-8"))
    current_hash = h.hexdigest()

    hash_file = proto_path.parent / ".mcubridge.proto.hash"
    outputs_exist = all(output.is_file() for output in expected_generation_outputs(args, repo_root=repo_root))
    up_to_date = outputs_exist and hash_file.is_file() and hash_file.read_text(encoding="utf-8").strip() == current_hash
    return up_to_date, hash_file, current_hash


def _dispatch_generated_file(
    src: Path,
    targets: list[Path | None],
    transform: Callable[[str], str] | None = None,
) -> None:
    if not src.exists():
        raise FileNotFoundError(f"Expected generated protocol artifact is missing: {src}")
    data = transform(src.read_text(encoding="utf-8")).encode("utf-8") if transform else src.read_bytes()
    for target_dir in filter(None, targets):
        target = target_dir / src.name
        if not target.is_file() or target.read_bytes() != data:
            target.write_bytes(data)
    src.unlink(missing_ok=True)


@dataclass
class GenerationArgs:
    spec: Path
    cpp: Path | None
    cpp_structs: Path | None
    py: Path | None
    py_client: Path | None


cli = typer.Typer(help="Protocol binding generator for MCU Bridge v2.", add_completion=False)


@cli.command()
def main(
    spec_file: Annotated[Path, typer.Option("--spec", help="Protocol specification file (.proto)")],
    cpp: Annotated[Path | None, typer.Option("--cpp", help="C++ header output")] = None,
    cpp_structs: Annotated[Path | None, typer.Option("--cpp-structs", help="C++ structs output")] = None,
    py: Annotated[Path | None, typer.Option("--py", help="Python output")] = None,
    py_client: Annotated[Path | None, typer.Option("--py-client", help="Python client output")] = None,
) -> None:
    ensure_nanopb_core_files()

    args = GenerationArgs(spec=spec_file, cpp=cpp, cpp_structs=cpp_structs, py=py, py_client=py_client)

    gen = JinjaGenerator()
    version = VERSION_PATH.read_text(encoding="utf-8").strip() if VERSION_PATH.exists() else "0.0.0"
    if version == "0.0.0":
        sys.stderr.write(f"Warning: VERSION file not found at {VERSION_PATH}, using fallback.\n")

    update_metadata(version)
    proto_path = _resolve_proto_path(args.spec)
    if not proto_path.is_file():
        raise FileNotFoundError(f"Protocol specification not found: {proto_path}")

    up_to_date, hash_file, current_hash = check_incremental_build(args, version)
    if up_to_date:
        sys.stderr.write("Protocol bindings up-to-date, skipping generation.\n")
        return

    sys.stderr.write(f"Formatting and compiling {proto_path} via buf...\n")
    run_buf_format(proto_path.parent)
    _, hash_file, current_hash = check_incremental_build(args, version)
    run_buf_generate(proto_path.parent)

    # Now load the compiled descriptor
    proto_spec = load_spec_from_proto(proto_path)
    context = build_protocol_context(proto_spec, version)

    # Dispatch compiled protobuf and stub artifacts
    py_targets = [args.py.parent if args.py else None, args.py_client.parent if args.py_client else None]
    cpp_targets = [args.cpp.parent if args.cpp else None]

    _dispatch_generated_file(
        proto_path.parent / "mcubridge.pb.h",
        cpp_targets,
        lambda t: t.replace("#include <pb.h>", '#include "../pb.h"'),
    )
    _dispatch_generated_file(proto_path.parent / "mcubridge.pb.c", cpp_targets)
    _dispatch_generated_file(proto_path.parent / "mcubridge_pb2.py", py_targets)
    _dispatch_generated_file(proto_path.parent / "mcubridge_pb2.pyi", py_targets)

    def _sanitize_grpc_bindings(text: str) -> str:
        text = text.replace("import mcubridge_pb2", "from . import mcubridge_pb2")
        return text.replace("if typing.TYPE_CHECKING:\n    import grpclib.server", "import grpclib.server")

    _dispatch_generated_file(
        proto_path.parent / "mcubridge_grpc.py",
        py_targets,
        _sanitize_grpc_bindings,
    )

    # Render Jinja2 artifacts declaratively
    py_context = context | {
        "constants": context["python_constants"],
        "handshake_constants": context["python_handshake_constants"],
    }
    py_client_context = context | {"constants": context["client_constants"]}
    template_contexts = {"common": context, "python": py_context, "client": py_client_context}
    explicit_outputs = (args.cpp, args.cpp_structs, args.py, args.py_client)
    for template_name, context_name, target_path, executable, is_py in _template_target_specs(args, REPO_ROOT):
        if target_path and (
            target_path.parent.exists()
            or target_path in explicit_outputs
            or target_path.parent in {path.parent for path in explicit_outputs if path}
        ):
            gen.render_template(
                template_name,
                template_contexts[context_name],
                target_path,
                create_parent=True,
                executable=executable,
                format_python=is_py,
            )
            sys.stderr.write(f"Generated {target_path}\n")

    missing_outputs = [output for output in expected_generation_outputs(args) if not output.is_file()]
    if missing_outputs:
        missing_list = ", ".join(str(output) for output in missing_outputs)
        raise RuntimeError(f"Protocol generation did not create expected artifacts: {missing_list}")

    # Save hash for incremental compilation
    hash_file.write_text(current_hash, encoding="utf-8")


if __name__ == "__main__":
    cli()
