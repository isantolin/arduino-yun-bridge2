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
import re
import shutil
import subprocess
import sys
import urllib.error
import urllib.request
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Annotated, Any, cast

import black
import tenacity
import typer
from google.protobuf.descriptor import FieldDescriptor
from google.protobuf.json_format import MessageToDict
from jinja2 import Environment, FileSystemLoader
from packaging.version import Version

# ═════════════════════════════════════════════════════════════════════════════
# DEPENDENCY VALIDATION (CRITICAL)
# ═════════════════════════════════════════════════════════════════════════════
REQUIRED_DEPS = ["jinja2", "google.protobuf", "nanopb", "mypy_protobuf", "grpclib"]

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
        sys.stderr.write(f"Warning: buf format failed:\n{e.stderr}\n")


def run_buf_generate(proto_dir: Path) -> None:
    """Run ``buf generate`` declaratively (replaces manual protoc/nanopb subprocess calls).

    Buf orchestrates all protoc plugins defined in ``buf.gen.yaml``:
    python pb2, mypy stubs, grpclib, and nanopb C.
    """
    import os

    env = os.environ.copy()
    # Ensure nanopb's bundled protoc is reachable for buf's protoc_builtin plugins
    nanopb_dir = _resolve_nanopb_protoc_dir()
    if nanopb_dir:
        env["PATH"] = f"{nanopb_dir}:{env.get('PATH', '')}"

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


EXPLICIT_CMD_TO_PB_CLASS: dict[str, str] = {
    "CMD_GET_VERSION_RESP": "VersionResponse",
    "CMD_GET_FREE_MEMORY_RESP": "FreeMemoryResponse",
    "CMD_GET_CAPABILITIES_RESP": "Capabilities",
    "CMD_SET_PIN_MODE": "PinMode",
    "CMD_SET_BAUDRATE": "SetBaudratePacket",
    "CMD_DIGITAL_READ": "PinRead",
    "CMD_ANALOG_READ": "PinRead",
    "CMD_PIN_SUBSCRIBE": "PinSubscribeRequest",
    "CMD_SPI_SET_CONFIG": "SpiConfig",
    "CMD_CLOCK_SYNC": "ClockSyncRequest",
    "CMD_LINK_SYNC_RESP": "LinkSync",
}


def cmd_name_to_pb_class(cmd_name: str) -> str:
    """Convert CMD_X_Y style command name to CamelCase class name."""
    clean_name = cmd_name.removeprefix("CMD_")
    return "".join("Response" if seg == "RESP" else seg.capitalize() for seg in clean_name.split("_"))


def resolve_cmd_to_pb_class(cmd_name: str, pb_module: Any) -> str | None:
    """Resolve command name to its canonical Protobuf message class name. [SIL-2]"""
    if (explicit := EXPLICIT_CMD_TO_PB_CLASS.get(cmd_name)) and hasattr(pb_module, explicit):
        return explicit
    candidate = cmd_name_to_pb_class(cmd_name)
    if hasattr(pb_module, candidate):
        return candidate
    return None


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


@dataclass
class StatusDef:
    name: str
    value: int
    description: str


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
            description=val.GetOptions().Extensions[mcubridge_pb2.status_opts].description,
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


def _build_command_context(spec: ProtocolSpec, pb_module: Any) -> dict[str, Any]:
    commands_by_name = {command.name for command in spec.commands}
    request_response_pairs: dict[str, list[str]] = {}
    response_to_req_map: dict[str, str] = {}
    for command in spec.commands:
        if command.name.endswith("_RESP"):
            request_name = command.name.removesuffix("_RESP")
            if request_name in commands_by_name:
                request_response_pairs.setdefault(request_name, []).append(command.name)
                response_to_req_map[command.name] = request_name
    command_to_pb = [
        (command.name, class_name)
        for command in spec.commands
        if (class_name := resolve_cmd_to_pb_class(command.name, pb_module)) is not None
    ]
    return {
        "request_response_pairs": request_response_pairs,
        "response_to_req_map": response_to_req_map,
        "command_to_pb": command_to_pb,
        "ack_commands": [command for command in spec.commands if command.requires_ack],
        "pre_sync_allowed_commands": [command for command in spec.commands if command.pre_sync_allowed],
        "response_only_commands": [command for command in spec.commands if command.expects_direct_response],
    }


def _build_descriptor_context(spec: ProtocolSpec, pb_module: Any) -> dict[str, Any]:
    file_desc = pb_module.DESCRIPTOR
    all_message_names = list(file_desc.message_types_by_name)
    options_path = (REPO_ROOT / "tools" / "protocol" / "mcubridge.options").resolve()
    options_content = options_path.read_text(encoding="utf-8")
    skipped_messages = set(re.findall(r"rpc\.pb\.(\w+)\s+skip_message:true", options_content))
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


def build_protocol_context(spec: ProtocolSpec, version: str) -> dict[str, Any]:
    """Build all template data from the protocol model and its descriptors."""
    constant_context = _build_constant_context(spec, version)
    handshake_context = _build_handshake_context(spec, spec.pb_module)
    action_context = _build_action_context(spec)
    command_context = _build_command_context(spec, spec.pb_module)
    descriptor_context = _build_descriptor_context(spec, spec.pb_module)
    telemetry_topic_field_map, telemetry_default_field = _build_telemetry_field_context(spec.pb_module)
    return {
        **constant_context,
        **handshake_context,
        **action_context,
        **command_context,
        **descriptor_context,
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
    }


class JinjaGenerator:
    def __init__(self) -> None:
        self.env = Environment(
            loader=FileSystemLoader(str(TEMPLATE_DIR)),
            keep_trailing_newline=True,
        )
        cast(dict[str, Any], self.env.filters)["cpp_digits"] = self._cpp_digit_separator
        cast(dict[str, Any], self.env.filters)["snakecase"] = self._snake_case

    @staticmethod
    def _cpp_digit_separator(value: object) -> str:
        """Format integers >= 10'000 with C++14 digit separators. [SIL-2]"""
        if not isinstance(value, int) or abs(value) < 10_000:
            return str(value)
        formatted = f"{abs(value):_}".replace("_", "'")
        return f"-{formatted}" if value < 0 else formatted

    @staticmethod
    def _snake_case(s: str) -> str:
        return re.sub(r"(?<!^)(?=[A-Z])", "_", s).lower()

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
            updated = re.sub(pattern, repl, target_path.read_text(encoding="utf-8"), count=count)
            target_path.write_text(updated, encoding="utf-8")
            sys.stderr.write(f"Updated {target_path} to version {version}\n")


def ensure_nanopb_core_files() -> None:
    """Ensure the core Nanopb C files exist in mcubridge-library-arduino/src/."""

    src_dir = REPO_ROOT / "mcubridge-library-arduino" / "src"
    version = "nanopb-0.4.9.2"
    base_url = f"https://raw.githubusercontent.com/nanopb/nanopb/{version}/"
    files = [
        "pb.h",
        "pb_common.h",
        "pb_common.c",
        "pb_decode.h",
        "pb_decode.c",
        "pb_encode.h",
        "pb_encode.c",
    ]

    src_dir.mkdir(parents=True, exist_ok=True)
    for f in files:
        target = src_dir / f
        if not target.exists():
            url = base_url + f
            sys.stderr.write(f"Downloading core Nanopb file: {f} from {url}...\n")
            retryer = tenacity.Retrying(
                stop=tenacity.stop_after_attempt(3),
                wait=tenacity.wait_exponential(multiplier=1.0, min=1.0, max=5.0),
                retry=tenacity.retry_if_exception_type((urllib.error.URLError, OSError, TimeoutError)),
                reraise=True,
            )

            def _fetch_nanopb_file(target_url: str = url) -> bytes:
                with urllib.request.urlopen(target_url, timeout=20) as response:
                    return response.read()

            try:
                content = retryer(_fetch_nanopb_file)
                target.write_bytes(content)
            except (
                urllib.error.URLError,
                OSError,
                TimeoutError,
                ValueError,
                tenacity.RetryError,
            ) as e:
                sys.stderr.write(f"Error downloading {f} after retries: {e}\n")
                sys.exit(1)


def check_incremental_build(args: Any, version: str) -> tuple[bool, Path, str]:
    proto_path = args.spec.resolve()
    h = hashlib.sha256()
    h.update(proto_path.read_bytes())
    h.update(Path(__file__).resolve().read_bytes())
    h.update(version.encode("utf-8"))
    templates_dir = Path(__file__).resolve().parent / "templates"
    if templates_dir.exists():
        for t_file in sorted(templates_dir.glob("*.j2")):
            h.update(t_file.read_bytes())
    current_hash = h.hexdigest()

    hash_file = proto_path.parent / ".mcubridge.proto.hash"

    # Check if all output files exist
    outputs = [args.cpp, args.cpp_structs, args.py, args.py_client]
    outputs_exist = all(out.exists() for out in outputs if out)

    # Also check if mcubridge_pb2.py exists in target locations
    if outputs_exist:
        if args.py and not (args.py.parent / "mcubridge_pb2.py").exists():
            outputs_exist = False
        if args.py_client and not (args.py_client.parent / "mcubridge_pb2.py").exists():
            outputs_exist = False

    up_to_date = bool(outputs_exist and hash_file.exists() and hash_file.read_text().strip() == current_hash)
    return up_to_date, hash_file, current_hash


def _dispatch_generated_file(
    src: Path,
    targets: list[Path | None],
    transform: Callable[[str], str] | None = None,
) -> None:
    if not src.exists():
        return
    data = transform(src.read_text(encoding="utf-8")).encode("utf-8") if transform else src.read_bytes()
    for target_dir in filter(None, targets):
        (target_dir / src.name).write_bytes(data)
    src.unlink(missing_ok=True)


@dataclass
class GenerationArgs:
    spec: Path
    cpp: Path | None
    cpp_structs: Path | None
    py: Path | None
    py_client: Path | None


cli = typer.Typer(help="Protocol binding generator for MCU Bridge v2.", add_completion=False)


def ensure_vulture_stub() -> None:
    """[Option B] Automatically generate PEP 561 type stubs for vulture in typings/."""
    vulture_dir = REPO_ROOT / "typings" / "vulture"
    vulture_dir.mkdir(parents=True, exist_ok=True)
    stub_file = vulture_dir / "__init__.pyi"
    stub_file.write_text(
        '"""[AUTO-GENERATED] Type stub for vulture package (SIL-2 / PEP 561)."""\n\n'
        "from collections.abc import Sequence\n\n"
        "class Item:\n"
        "    name: str\n"
        "    filename: str\n"
        "    first_lineno: int\n"
        "    last_lineno: int\n"
        "    message: str\n"
        "    confidence: int\n\n"
        "class Vulture:\n"
        "    def __init__(\n"
        "        self,\n"
        "        verbose: bool = False,\n"
        "        ignore_names: Sequence[str] | None = None,\n"
        "        ignore_decorators: Sequence[str] | None = None,\n"
        "    ) -> None: ...\n"
        "    def scavenge(self, paths: Sequence[str], exclude: Sequence[str] | None = None) -> None: ...\n"
        '    def scan(self, code: str, filename: str = "") -> None: ...\n'
        "    def get_unused_code(self, min_confidence: int = 0, sort_by_size: bool = False) -> list[Item]: ...\n\n"
        '__all__ = ["Vulture", "Item"]\n',
        encoding="utf-8",
    )


@cli.command()
def main(
    spec_file: Annotated[Path, typer.Option("--spec", help="Protocol specification file (.proto)")],
    cpp: Annotated[Path | None, typer.Option("--cpp", help="C++ header output")] = None,
    cpp_structs: Annotated[Path | None, typer.Option("--cpp-structs", help="C++ structs output")] = None,
    py: Annotated[Path | None, typer.Option("--py", help="Python output")] = None,
    py_client: Annotated[Path | None, typer.Option("--py-client", help="Python client output")] = None,
) -> None:
    ensure_nanopb_core_files()
    ensure_vulture_stub()

    args = GenerationArgs(spec=spec_file, cpp=cpp, cpp_structs=cpp_structs, py=py, py_client=py_client)

    gen = JinjaGenerator()
    version = VERSION_PATH.read_text(encoding="utf-8").strip() if VERSION_PATH.exists() else "0.0.0"
    if version == "0.0.0":
        sys.stderr.write(f"Warning: VERSION file not found at {VERSION_PATH}, using fallback.\n")

    up_to_date, hash_file, current_hash = check_incremental_build(args, version)
    if up_to_date:
        sys.stderr.write("Protocol bindings up-to-date, skipping generation.\n")
        return

    update_metadata(version)

    # Compile the protobuf via buf generate (declarative, replaces manual protoc/nanopb subprocess calls)
    proto_path = args.spec.resolve()
    if proto_path.suffix == ".toml":
        proto_path = (proto_path.parent / "mcubridge.proto").resolve()

    if proto_path.exists():
        sys.stderr.write(f"Formatting and compiling {proto_path} via buf...\n")
        run_buf_format(proto_path.parent)
        run_buf_generate(proto_path.parent)

    # Now load the compiled descriptor
    proto_spec = load_spec_from_proto(proto_path)
    context = build_protocol_context(proto_spec, version)

    # Dispatch compiled protobuf and stub artifacts
    if proto_path.exists():
        py_targets = [args.py.parent if args.py else None, args.py_client.parent if args.py_client else None]
        cpp_targets = [args.cpp.parent if args.cpp else None]

        _dispatch_generated_file(
            proto_path.parent / "mcubridge.pb.h",
            cpp_targets,
            lambda t: t.replace("#include <pb.h>", '#include "../pb.h"'),
        )
        _dispatch_generated_file(proto_path.parent / "mcubridge.pb.c", cpp_targets)
        _dispatch_generated_file(proto_path.parent / "mcubridge_pb2.py", py_targets)
        _dispatch_generated_file(
            proto_path.parent / "mcubridge_pb2.pyi",
            py_targets,
            lambda t: t.replace(
                "_Union[StructuredEntry, _Mapping]]",
                "_Union[StructuredEntry, _Mapping[str, object]]]",
            ),
        )
        _dispatch_generated_file(
            proto_path.parent / "mcubridge_grpc.py",
            py_targets,
            lambda t: t.replace("import mcubridge_pb2", "from . import mcubridge_pb2"),
        )

    # Render Jinja2 artifacts declaratively
    py_context = context | {
        "constants": context["python_constants"],
        "handshake_constants": context["python_handshake_constants"],
    }
    py_client_context = context | {"constants": context["client_constants"]}

    targets: list[tuple[str, dict[str, Any], Path | None, bool, bool]] = [
        ("rpc_protocol.h.j2", context, args.cpp, False, False),
        ("rpc_hw_config.h.j2", context, args.cpp.parent / "rpc_hw_config.h" if args.cpp else None, False, False),
        ("rpc_structs.h.j2", context, args.cpp_structs, False, False),
        ("protocol.py.j2", py_context, args.py, False, True),
        ("protocol_client.py.j2", py_client_context, args.py_client, False, True),
        (
            "mcubridge_uci.j2",
            context,
            REPO_ROOT / "luci-app-mcubridge" / "root" / "etc" / "config" / "mcubridge",
            False,
            False,
        ),
        (
            "defaults_sh.j2",
            context,
            REPO_ROOT / "mcubridge" / "scripts" / "defaults.sh",
            True,
            False,
        ),
        (
            "config_schema_json.j2",
            context,
            REPO_ROOT
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

    for template_name, ctx, target_path, executable, is_py in targets:
        if target_path and (
            target_path.parent.exists() or target_path in (args.cpp, args.cpp_structs, args.py, args.py_client)
        ):
            gen.render_template(
                template_name,
                ctx,
                target_path,
                create_parent=True,
                executable=executable,
                format_python=is_py,
            )
            sys.stderr.write(f"Generated {target_path}\n")

    # Save hash for incremental compilation
    hash_file.write_text(current_hash, encoding="utf-8")


if __name__ == "__main__":
    cli()
